"""One-shot application owner for ordered production shutdown."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.service.managed_input_contracts import (
    ManagedInputCloseResult,
    ManagedInputCloseStatus,
)
from app.service.multi_run_coordinator_mvp.contracts import (
    CoordinatorShutdownReport,
    CoordinatorShutdownStatus,
)
from app.service.multi_run_coordinator_mvp.managed_resident_capture_port import (
    ManagedResidentCaptureCloseReport,
    ManagedResidentCaptureCloseStatus,
)
from app.service.plc_result_sequencer import PlcShutdownReport
from app.service.run_persistence.run_persistence_scheduler_bridge import (
    BridgeDatabaseCloseResult,
    BridgeDatabaseCloseStatus,
    BridgeSchedulerShutdownResult,
    BridgeSchedulerShutdownStatus,
)
from app.service.run_scoped_playback.contracts import (
    PlaybackLifecycleCode,
    PlaybackLifecycleReport,
    PlaybackLifecycleStatus,
)


class ApplicationShutdownStageStatus(str, Enum):
    CLEAN = "CLEAN"
    NON_CLEAN = "NON_CLEAN"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class ApplicationShutdownStageReport:
    name: str
    status: ApplicationShutdownStageStatus
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("shutdown stage name must be nonempty text")
        if type(self.status) is not ApplicationShutdownStageStatus:
            raise ValueError("shutdown stage status must be exact")
        if self.detail is not None and (
            type(self.detail) is not str or not self.detail
        ):
            raise ValueError("shutdown stage detail must be nonempty text")


@dataclass(frozen=True, slots=True)
class ApplicationShutdownReport:
    clean: bool
    restart_required: bool
    deadline_monotonic: float
    stages: tuple[ApplicationShutdownStageReport, ...]
    errors: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.clean) is not bool:
            raise ValueError("clean must be an exact bool")
        if type(self.restart_required) is not bool:
            raise ValueError("restart_required must be an exact bool")
        if (
            type(self.deadline_monotonic) is not float
            or not math.isfinite(self.deadline_monotonic)
            or self.deadline_monotonic < 0.0
        ):
            raise ValueError("deadline_monotonic must be finite and nonnegative")
        if type(self.stages) is not tuple or any(
            type(item) is not ApplicationShutdownStageReport
            for item in self.stages
        ):
            raise ValueError("stages must contain exact stage reports")
        if type(self.errors) is not tuple or any(
            type(item) is not str or not item for item in self.errors
        ):
            raise ValueError("errors must be nonempty text values")
        if self.clean == self.restart_required:
            raise ValueError("clean and restart_required must be opposites")
        if self.clean and (
            self.errors
            or any(
                item.status is ApplicationShutdownStageStatus.NON_CLEAN
                for item in self.stages
            )
        ):
            raise ValueError("clean shutdown cannot contain failure evidence")


@dataclass(frozen=True, slots=True)
class ApplicationShutdownResources:
    coordinator: object | None = None
    scheduler_bridge: object | None = None
    external_producers: tuple[object, ...] = ()
    plc_sequencer: object | None = None
    plc_owners: tuple[object, ...] = ()
    resident_capture: object | None = None
    managed_resident: object | None = None
    playback: object | None = None
    independent_writers: tuple[object, ...] = ()
    application_config_writers: tuple[object, ...] = ()
    application_config_database: object | None = None
    shared_audio_backend: object | None = None
    active_completion: object | None = None
    prepared_composition: object | None = None
    run_persistence_database: object | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "external_producers",
            "plc_owners",
            "independent_writers",
            "application_config_writers",
        ):
            if type(getattr(self, field_name)) is not tuple:
                raise ValueError(f"{field_name} must be an exact tuple")


@dataclass(frozen=True, slots=True)
class PlcServiceThreadShutdownOwner:
    """One PLC's split ingress, connection, and thread shutdown owner."""

    service: object
    thread: object | None
    invoke_thread_slot: Callable[[object, object, str], None]

    def stop_ingress_polling(self) -> None:
        self._invoke("stop_ingress_polling")

    def disconnect_and_join(self, timeout_seconds: float) -> None:
        timeout = _valid_timeout(timeout_seconds)
        thread = self.thread
        if thread is None:
            raise RuntimeError("PLC thread is absent")
        self._invoke("disconnect_plc_service")
        quit_method = getattr(thread, "quit", None)
        wait_method = getattr(thread, "wait", None)
        if not callable(quit_method) or not callable(wait_method):
            raise TypeError("PLC thread has no quit/wait lifecycle")
        if quit_method() is not None:
            raise TypeError("PLC thread quit returned a malformed value")
        if wait_method(max(0, int(timeout * 1000.0))) is not True:
            raise TimeoutError("PLC thread did not join before deadline")

    def _invoke(self, method_name: str) -> None:
        if self.thread is None:
            raise RuntimeError("PLC thread is absent")
        is_running = getattr(self.thread, "isRunning", None)
        if not callable(is_running):
            raise TypeError("PLC thread has no running-state boundary")
        if is_running() is not True:
            raise RuntimeError("PLC thread is not running")
        if not callable(self.invoke_thread_slot):
            raise TypeError("PLC thread-affine slot invoker is not callable")
        if (
            self.invoke_thread_slot(
                self.service,
                self.thread,
                method_name,
            )
            is not None
        ):
            raise TypeError("PLC thread-affine slot invoker returned a value")


def _bounded_detail(value: object) -> str:
    text = str(value).strip() or type(value).__name__
    encoded = text.encode("utf-8")[:512]
    return encoded.decode("utf-8", errors="ignore") or "shutdown failure"


def _valid_timeout(value: object) -> float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise ValueError("timeout_seconds must be finite and nonnegative")
    return float(value)


class ApplicationShutdownOwner:
    """Serializes all application exit paths and caches their one report."""

    def __init__(
        self,
        resources: ApplicationShutdownResources | Callable[
            [], ApplicationShutdownResources
        ],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 5.0,
        aggregate_notice: Callable[[ApplicationShutdownReport], None] | None = None,
    ) -> None:
        if not callable(monotonic):
            raise ValueError("monotonic must be callable")
        if aggregate_notice is not None and not callable(aggregate_notice):
            raise ValueError("aggregate_notice must be callable")
        self._resources = resources
        self._monotonic = monotonic
        self._timeout_seconds = _valid_timeout(timeout_seconds)
        self._aggregate_notice = aggregate_notice
        self._lock = threading.Lock()
        self._report: ApplicationShutdownReport | None = None
        self._deadline = 0.0
        self._last_remaining = 0.0
        self._stages: list[ApplicationShutdownStageReport] = []
        self._errors: list[str] = []

    def shutdown(
        self,
        timeout_seconds: float | None = None,
    ) -> ApplicationShutdownReport:
        timeout = (
            self._timeout_seconds
            if timeout_seconds is None
            else _valid_timeout(timeout_seconds)
        )
        with self._lock:
            if self._report is not None:
                return self._report
            try:
                started = float(self._monotonic())
                if not math.isfinite(started) or started < 0.0:
                    raise ValueError("monotonic clock returned an invalid value")
            except Exception as exc:
                started = 0.0
                timeout = 0.0
                self._record_non_clean("deadline", exc)
            self._deadline = started + timeout
            self._last_remaining = timeout
            try:
                resources = (
                    self._resources()
                    if callable(self._resources)
                    else self._resources
                )
                if type(resources) is not ApplicationShutdownResources:
                    raise TypeError(
                        "shutdown resource provider returned a malformed value"
                    )
            except Exception as exc:
                self._record_non_clean("resources", exc)
                resources = ApplicationShutdownResources()
            try:
                self._run(resources)
            finally:
                # A child process is an independently owned resource. Earlier
                # fail-closed gates still protect in-process consumers and DBs,
                # but must never leave this owner's capture child alive.
                if callable(getattr(resources.managed_resident, "reap_owned_capture_process", None)):
                    self._call(
                        "capture_process.reap",
                        resources.managed_resident,
                        ("reap_owned_capture_process",),
                        (self._remaining(),),
                        lambda result: result is True,
                    )
            clean = not self._errors and not any(
                stage.status is ApplicationShutdownStageStatus.NON_CLEAN
                for stage in self._stages
            )
            report = ApplicationShutdownReport(
                clean=clean,
                restart_required=not clean,
                deadline_monotonic=float(self._deadline),
                stages=tuple(self._stages),
                errors=tuple(self._errors[:32]),
            )
            self._report = report
            if self._aggregate_notice is not None:
                try:
                    self._aggregate_notice(report)
                except Exception:
                    pass
            return report

    def _remaining(self) -> float:
        try:
            now = float(self._monotonic())
            if not math.isfinite(now) or now < 0.0:
                raise ValueError("monotonic clock returned an invalid value")
            remaining = max(0.0, self._deadline - now)
        except Exception as exc:
            self._record_non_clean("deadline", exc)
            remaining = 0.0
        remaining = min(self._last_remaining, remaining)
        self._last_remaining = remaining
        return remaining

    def _record(
        self,
        name: str,
        status: ApplicationShutdownStageStatus,
        detail: str | None = None,
    ) -> None:
        self._stages.append(ApplicationShutdownStageReport(name, status, detail))

    def _record_non_clean(self, name: str, value: object) -> None:
        detail = _bounded_detail(value)
        self._record(name, ApplicationShutdownStageStatus.NON_CLEAN, detail)
        self._errors.append(f"{name}: {detail}")

    def _skip(self, name: str, detail: str) -> None:
        self._record(name, ApplicationShutdownStageStatus.SKIPPED, detail)

    def _call(
        self,
        name: str,
        resource: object | None,
        method_names: tuple[str, ...],
        args: tuple[object, ...],
        validator: Callable[[object], bool],
        *,
        required: bool = True,
    ) -> bool:
        if resource is None:
            if required:
                self._record_non_clean(name, "required active owner is missing")
                return False
            self._record(name, ApplicationShutdownStageStatus.SKIPPED, "not owned")
            return True
        method = next(
            (
                getattr(resource, candidate, None)
                for candidate in method_names
                if callable(getattr(resource, candidate, None))
            ),
            None,
        )
        if method is None and callable(resource):
            method = resource
        if method is None:
            self._record_non_clean(name, "required shutdown method is missing")
            return False
        try:
            result = method(*args)
        except Exception as exc:
            self._record_non_clean(name, exc)
            return False
        try:
            clean = validator(result)
        except Exception as exc:
            self._record_non_clean(name, exc)
            return False
        if clean is not True:
            self._record_non_clean(name, "non-clean or malformed result")
            return False
        self._record(name, ApplicationShutdownStageStatus.CLEAN)
        return True

    def _call_many(
        self,
        name: str,
        resources: tuple[object, ...],
        method_names: tuple[str, ...],
    ) -> bool:
        clean = True
        if not resources:
            self._record(name, ApplicationShutdownStageStatus.SKIPPED, "not owned")
            return True
        for index, resource in enumerate(resources):
            clean = (
                self._call(
                    f"{name}[{index}]",
                    resource,
                    method_names,
                    (),
                    lambda result: result is None,
                )
                and clean
            )
        return clean

    def _close_plc_owner(self, name: str, resource: object) -> bool:
        disconnect_and_join = getattr(resource, "disconnect_and_join", None)
        if callable(disconnect_and_join):
            return self._call(
                name,
                resource,
                ("disconnect_and_join",),
                (self._remaining(),),
                lambda result: result is None,
            )
        quit_method = getattr(resource, "quit", None)
        wait_method = getattr(resource, "wait", None)
        if callable(quit_method) and callable(wait_method):
            try:
                quit_result = quit_method()
                if quit_result is not None:
                    raise TypeError("PLC thread quit returned a malformed value")
                wait_milliseconds = max(
                    0,
                    int(self._remaining() * 1000.0),
                )
                wait_result = wait_method(wait_milliseconds)
                if wait_result is not True:
                    raise TimeoutError("PLC thread did not join before deadline")
            except Exception as exc:
                self._record_non_clean(name, exc)
                return False
            self._record(name, ApplicationShutdownStageStatus.CLEAN)
            return True
        return self._call(
            name,
            resource,
            ("close", "shutdown", "disconnect"),
            (),
            lambda result: result is None,
        )

    def _skip_active_tail(self, names: tuple[str, ...], reason: str) -> None:
        for name in names:
            self._skip(name, reason)

    def _run(self, resources: ApplicationShutdownResources) -> None:
        if resources.coordinator is None:
            if resources.prepared_composition is None:
                prepared_clean = True
                self._record(
                    "prepared_composition",
                    ApplicationShutdownStageStatus.SKIPPED,
                    "startup did not acquire a prepared graph",
                )
            else:
                prepared_clean = self._call(
                    "prepared_composition",
                    resources.prepared_composition,
                    ("close",),
                    (),
                    lambda result: result is None,
                )
            self._run_after_graph(
                resources,
                graph_prefix_clean=prepared_clean,
                prepared_only=True,
            )
            return

        begin_clean = self._call(
            "coordinator.begin_shutdown",
            resources.coordinator,
            ("begin_shutdown",),
            (),
            lambda result: result is None,
        )
        if not begin_clean:
            self._skip_active_tail(
                (
                    "bridge.phase_a",
                    "coordinator.finish_shutdown",
                    "external_producers",
                    "plc_sequencer",
                    "plc_owners",
                    "resident_capture",
                    "managed_resident",
                    "playback",
                    "independent_writers",
                    "application_config_writers",
                    "application_config_database",
                    "shared_audio_backend",
                    "bridge.phase_b",
                ),
                "Coordinator begin was not exact-clean",
            )
            return
        bridge_clean = self._call(
            "bridge.phase_a",
            resources.scheduler_bridge,
            ("stop_and_join_scheduler",),
            (self._remaining(),),
            lambda result: (
                type(result) is BridgeSchedulerShutdownResult
                and result.status
                is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
            ),
        )
        if not bridge_clean:
            self._skip_active_tail(
                (
                    "coordinator.finish_shutdown",
                    "external_producers",
                    "plc_sequencer",
                    "plc_owners",
                    "resident_capture",
                    "managed_resident",
                    "playback",
                    "independent_writers",
                    "application_config_writers",
                    "application_config_database",
                    "shared_audio_backend",
                    "bridge.phase_b",
                ),
                "Bridge Phase A was not exact-clean",
            )
            return
        finish_clean = self._call(
            "coordinator.finish_shutdown",
            resources.coordinator,
            ("finish_shutdown",),
            (self._deadline,),
            lambda result: (
                type(result) is CoordinatorShutdownReport
                and result.status is CoordinatorShutdownStatus.CLOSED
            ),
        )
        if not finish_clean:
            self._call_many(
                "external_producers",
                resources.external_producers,
                ("stop_ingress_polling", "close", "shutdown", "stop"),
            )
            self._skip_active_tail(
                (
                    "plc_sequencer",
                    "plc_owners",
                    "resident_capture",
                    "managed_resident",
                    "playback",
                    "independent_writers",
                    "application_config_writers",
                    "application_config_database",
                    "shared_audio_backend",
                    "bridge.phase_b",
                ),
                "Coordinator finish was not exact-clean",
            )
            return
        self._run_after_graph(
            resources,
            graph_prefix_clean=True,
        )

    def _run_after_graph(
        self,
        resources: ApplicationShutdownResources,
        *,
        graph_prefix_clean: bool,
        scheduler_closed: bool = True,
        prepared_only: bool = False,
    ) -> None:
        if prepared_only:
            writers_clean = self._call_many(
                "independent_writers",
                resources.independent_writers,
                ("shutdown", "close", "stop_camera", "stop"),
            )
            config_writers_clean = self._call_many(
                "application_config_writers",
                resources.application_config_writers,
                ("close", "shutdown", "stop"),
            )
            config_db_clean = (
                self._call(
                    "application_config_database",
                    resources.application_config_database,
                    ("close",),
                    (),
                    lambda result: result is None,
                )
                if config_writers_clean
                else False
            )
            if not config_writers_clean:
                self._skip(
                    "application_config_database",
                    "an application-config writer was not clean",
                )
            all_prior_clean = all(
                (
                    graph_prefix_clean,
                    writers_clean,
                    config_writers_clean,
                    config_db_clean,
                )
            )
            if all_prior_clean:
                self._call(
                    "run_database.phase_b",
                    resources.run_persistence_database,
                    ("close_after_upstream_stopped",),
                    (),
                    lambda result: result is None,
                )
            else:
                self._skip(
                    "run_database.phase_b",
                    "a required prior shutdown stage was not exact-clean",
                )
            return

        producer_results: list[bool] = []
        if resources.external_producers:
            for index, resource in enumerate(resources.external_producers):
                producer_results.append(
                    self._call(
                        f"external_producers[{index}]",
                        resource,
                        ("stop_ingress_polling", "close", "shutdown", "stop"),
                        (),
                        lambda result: result is None,
                    )
                )
            producers_clean = all(producer_results)
        else:
            producers_clean = True
            self._record(
                "external_producers",
                ApplicationShutdownStageStatus.SKIPPED,
                "not owned",
            )
        plc_clean = self._call(
            "plc_sequencer",
            resources.plc_sequencer,
            ("shutdown",),
            (self._remaining(),),
            lambda result: (
                type(result) is PlcShutdownReport and result.clean is True
            ),
        )
        owner_results: list[bool] = []
        if resources.plc_owners:
            for index, resource in enumerate(resources.plc_owners):
                if index < len(producer_results) and not producer_results[index]:
                    self._skip(
                        f"plc_owners[{index}]",
                        "its PLC-start producer was not clean",
                    )
                    owner_results.append(False)
                    continue
                if not plc_clean:
                    self._skip(
                        f"plc_owners[{index}]",
                        "PLC sequencer was not clean",
                    )
                    owner_results.append(False)
                    continue
                owner_results.append(
                    self._close_plc_owner(f"plc_owners[{index}]", resource)
                )
            plc_owners_clean = all(owner_results)
        else:
            plc_owners_clean = True
            self._record(
                "plc_owners",
                ApplicationShutdownStageStatus.SKIPPED,
                "not owned",
            )

        if scheduler_closed:
            capture_clean = self._call(
                "resident_capture",
                resources.resident_capture,
                ("close_and_join",),
                (self._remaining(),),
                lambda result: (
                    type(result) is ManagedResidentCaptureCloseReport
                    and result.status
                    is ManagedResidentCaptureCloseStatus.CLOSED
                ),
            )
            if capture_clean:
                resident_clean = self._call(
                    "managed_resident",
                    resources.managed_resident,
                    ("close_all", "shutdown"),
                    (self._remaining(),),
                    lambda result: (
                        type(result) is tuple
                        and 1 <= len(result) <= 6
                        and all(
                            type(item) is ManagedInputCloseResult
                            and item.status
                            in (
                                ManagedInputCloseStatus.CLOSED,
                                ManagedInputCloseStatus.NOT_OPEN,
                            )
                            for item in result
                        )
                    ),
                )
            else:
                resident_clean = False
                self._skip(
                    "managed_resident",
                    "managed Resident capture was not clean",
                )
        else:
            capture_clean = False
            resident_clean = False
            self._skip("resident_capture", "Scheduler was not closed")
            self._skip("managed_resident", "Resident capture was not closed")

        playback_clean = self._call(
            "playback",
            resources.playback,
            ("shutdown",),
            (self._remaining(),),
            lambda result: (
                type(result) is PlaybackLifecycleReport
                and result.status is PlaybackLifecycleStatus.CLEAN
                and result.code is PlaybackLifecycleCode.CLEAN
            ),
        )
        writers_clean = self._call_many(
            "independent_writers",
            resources.independent_writers,
            ("shutdown", "close", "stop_camera", "stop"),
        )
        config_writers_clean = self._call_many(
            "application_config_writers",
            resources.application_config_writers,
            ("close", "shutdown", "stop"),
        )
        if config_writers_clean:
            config_db_clean = self._call(
                "application_config_database",
                resources.application_config_database,
                ("close",),
                (),
                lambda result: result is None,
            )
        else:
            config_db_clean = False
            self._skip(
                "application_config_database",
                "an application-config writer was not clean",
            )
        if resident_clean and playback_clean:
            backend_clean = self._call(
                "shared_audio_backend",
                resources.shared_audio_backend,
                ("close", "terminate", "shutdown"),
                (),
                lambda result: result is None,
                required=False,
            )
        else:
            backend_clean = False
            self._skip(
                "shared_audio_backend",
                "Resident and Playback were not both clean",
            )

        all_prior_clean = all(
            (
                graph_prefix_clean,
                producers_clean,
                plc_clean,
                plc_owners_clean,
                capture_clean,
                resident_clean,
                playback_clean,
                writers_clean,
                config_writers_clean,
                config_db_clean,
                backend_clean,
            )
        )
        if all_prior_clean:
            completion_clean = self._call(
                "active_completion",
                resources.active_completion,
                ("complete_ordered_shutdown",),
                (),
                lambda result: result is None,
                required=False,
            )
            all_prior_clean = all_prior_clean and completion_clean
        else:
            completion_clean = False
            self._skip(
                "active_completion",
                "a required owned resource was not exact-clean",
            )
        if all_prior_clean:
            phase_b_clean = self._call(
                "bridge.phase_b",
                resources.scheduler_bridge,
                ("close_persistence_after_upstream_stopped",),
                (),
                lambda result: (
                    type(result) is BridgeDatabaseCloseResult
                    and result.status
                    is BridgeDatabaseCloseStatus.DATABASE_CLOSED
                    and result.database_closed is True
                    and result.restart_required is False
                    and result.code is None
                ),
            )
        else:
            phase_b_clean = False
            self._skip(
                "bridge.phase_b",
                "a required prior shutdown stage was not exact-clean",
            )
        if not phase_b_clean and all_prior_clean:
            graph_prefix_clean = False


__all__ = [
    "ApplicationShutdownOwner",
    "ApplicationShutdownReport",
    "ApplicationShutdownResources",
    "ApplicationShutdownStageReport",
    "ApplicationShutdownStageStatus",
    "PlcServiceThreadShutdownOwner",
]
