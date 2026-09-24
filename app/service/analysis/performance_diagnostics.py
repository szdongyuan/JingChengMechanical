"""Bounded, payload-free timing diagnostics for staged analysis."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


BENCHMARK_TIMESTAMP_FIELDS = (
    "audio_frozen_ns",
    "decision_scheduled_ns",
    "audio_transport_ready_ns",
    "decision_features_finished_ns",
    "decision_dsp_finished_ns",
    "ai_finished_ns",
    "decision_projection_finished_ns",
    "durable_db_committed_ns",
    "plc_acknowledged_ns",
    "decision_painted_ns",
    "quiet_fence_released_ns",
    "enrichment_dsp_finished_ns",
    "canonical_encoded_ns",
    "compressed_ns",
    "transport_finished_ns",
    "artifact_fsync_finished_ns",
    "charts_painted_ns",
)

BENCHMARK_DURATION_FIELDS = (
    "decision_scheduling_ns",
    "audio_transport_ns",
    "decision_feature_extraction_ns",
    "decision_judgment_ns",
    "decision_dsp_ns",
    "ai_ns",
    "decision_projection_ns",
    "durable_db_ns",
    "plc_ns",
    "decision_paint_ns",
    "decision_endpoint_ns",
    "quiet_fence_ns",
    "enrichment_dsp_ns",
    "canonical_encoding_ns",
    "compression_ns",
    "transport_ns",
    "artifact_fsync_ns",
    "chart_paint_ns",
    "charts_endpoint_ns",
)

BENCHMARK_SIZE_FIELDS = (
    "canonical_json_bytes",
    "zlib_bytes",
    "bundle_peak_bytes",
)


def benchmark_percentiles(values: list[int]) -> dict[str, int]:
    """Return deterministic nearest-rank latency statistics."""

    if type(values) is not list:
        raise TypeError("values must be an exact list")
    if not values:
        raise ValueError("values must contain at least one sample")
    for value in values:
        if type(value) is not int:
            raise TypeError("benchmark samples must be exact int values")
        if value < 0:
            raise ValueError("benchmark samples must be non-negative")
    ordered = sorted(values)

    def nearest_rank(percentile: int) -> int:
        index = max(0, math.ceil(percentile * len(ordered) / 100) - 1)
        return ordered[index]

    return {
        "sample_count": len(ordered),
        "p50_ns": nearest_rank(50),
        "p95_ns": nearest_rank(95),
        "max_ns": ordered[-1],
    }


def _require_report_mapping(value: object, path: str) -> dict:
    if type(value) is not dict:
        raise ValueError(f"{path} must be an object")
    return value


def _require_exact_non_negative_int(value: object, path: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{path} must be a non-negative exact int")


def validate_benchmark_report(
    report: object,
    *,
    require_hard_sla: bool,
) -> None:
    """Validate observable SLA evidence without duplicating production schema."""

    root = _require_report_mapping(report, "report")
    if root.get("schema") != "analysis-latency-benchmark/v1":
        raise ValueError("unsupported benchmark report schema")
    host = _require_report_mapping(root.get("host"), "host")
    endpoints = _require_report_mapping(root.get("endpoints"), "endpoints")
    scenarios = _require_report_mapping(root.get("scenarios"), "scenarios")
    sla = _require_report_mapping(root.get("sla"), "sla")

    expected_scenarios = (
        "single_channel",
        "six_run_overlap",
        "two_channel_48khz",
    )
    for scenario_name in expected_scenarios:
        scenario = _require_report_mapping(
            scenarios.get(scenario_name),
            f"scenarios.{scenario_name}",
        )
        iterations = scenario.get("iterations")
        if type(iterations) is not list:
            raise ValueError(f"scenarios.{scenario_name}.iterations must be a list")
        minimum = 6 if scenario_name == "six_run_overlap" else 30
        if require_hard_sla and len(iterations) < minimum:
            raise ValueError(
                f"scenarios.{scenario_name} requires at least {minimum} warm iterations"
            )
        if require_hard_sla:
            oracle = _require_report_mapping(
                scenario.get("legacy_oracle"),
                f"scenarios.{scenario_name}.legacy_oracle",
            )
            if oracle.get("status") != "PASS":
                raise ValueError(
                    f"scenarios.{scenario_name} requires an independent legacy oracle PASS"
                )
        for index, iteration_value in enumerate(iterations):
            prefix = f"scenarios.{scenario_name}.iterations[{index}]"
            iteration = _require_report_mapping(iteration_value, prefix)
            timestamps = _require_report_mapping(
                iteration.get("timestamps_ns"), f"{prefix}.timestamps_ns"
            )
            durations = _require_report_mapping(
                iteration.get("durations_ns"), f"{prefix}.durations_ns"
            )
            sizes = _require_report_mapping(
                iteration.get("sizes_bytes"), f"{prefix}.sizes_bytes"
            )
            for field in BENCHMARK_TIMESTAMP_FIELDS:
                if field not in timestamps:
                    raise ValueError(f"{prefix}.timestamps_ns.{field} is missing")
                if timestamps[field] is None and not require_hard_sla:
                    continue
                _require_exact_non_negative_int(
                    timestamps[field], f"{prefix}.timestamps_ns.{field}"
                )
            for field in BENCHMARK_DURATION_FIELDS:
                if field not in durations:
                    raise ValueError(f"{prefix}.durations_ns.{field} is missing")
                if durations[field] is None and not require_hard_sla:
                    continue
                _require_exact_non_negative_int(
                    durations[field], f"{prefix}.durations_ns.{field}"
                )
            if durations["quiet_fence_ns"] < 250_000_000:
                raise ValueError(
                    f"{prefix} did not observe a fresh 250 ms quiet fence"
                )
            for field in BENCHMARK_SIZE_FIELDS:
                if field not in sizes:
                    raise ValueError(f"{prefix}.sizes_bytes.{field} is missing")
                _require_exact_non_negative_int(
                    sizes[field], f"{prefix}.sizes_bytes.{field}"
                )
            if type(iteration.get("steady_ai_process_spawned")) is not bool:
                raise ValueError(
                    f"{prefix}.steady_ai_process_spawned must be an exact bool"
                )

    if require_hard_sla:
        if sla.get("status") not in {"PASS", "FAIL"} or sla.get("evaluated") is not True:
            raise ValueError("hard SLA report must be evaluated as PASS or FAIL")
        if host.get("result_window_shown") is not True:
            raise ValueError("hard SLA requires a shown result window")
        if host.get("result_window_minimized") is not False:
            raise ValueError("hard SLA result window must not be minimized")
        plc = _require_report_mapping(endpoints.get("plc"), "endpoints.plc")
        if (
            plc.get("evaluated") is not True
            or plc.get("source")
            != "PlcResultAttemptReport.BOUNDARY_RETURNED"
        ):
            raise ValueError("hard SLA requires an actual PLC write boundary return")
        paint = _require_report_mapping(
            endpoints.get("decision_paint"), "endpoints.decision_paint"
        )
        if (
            paint.get("evaluated") is not True
            or paint.get("source") != "ResultModule.decisionPainted"
        ):
            raise ValueError("hard SLA requires ResultModule.decisionPainted")
        durable = _require_report_mapping(
            endpoints.get("durable_db"), "endpoints.durable_db"
        )
        if (
            durable.get("evaluated") is not True
            or durable.get("source")
            != "AnalysisPersistenceBoundary.accept_enrichment/EnrichmentCommittedEvent"
        ):
            raise ValueError(
                "hard SLA requires the production persistence committed event"
            )
    elif sla.get("evaluated") is not True:
        if sla.get("status") != "SLA_NOT_EVALUATED":
            raise ValueError("non-evaluated report must use SLA_NOT_EVALUATED")
        blockers = sla.get("blockers")
        if type(blockers) is not list or not blockers:
            raise ValueError("SLA_NOT_EVALUATED requires at least one blocker")


class AnalysisTimingBoundary(str, Enum):
    AUDIO_FROZEN = "audio_frozen"
    QUIET_GATE_RELEASED = "quiet_gate_released"
    AUDIO_TRANSPORT_READY = "audio_transport_ready"
    DECISION_FEATURE_EXTRACTION = "decision_feature_extraction"
    DECISION_JUDGMENT = "decision_judgment"
    LEGACY_DECISION_DSP = "legacy_decision_dsp"
    RESIDENT_AI_ACQUIRE = "resident_ai_acquire"
    RESIDENT_AI_SEND = "resident_ai_send"
    RESIDENT_AI_RESPONSE = "resident_ai_response"
    DECISION_PROJECTION = "decision_projection"
    ENRICHMENT_DSP = "enrichment_dsp"
    CANONICAL_ENCODING = "canonical_encoding"
    CANONICAL_COMPRESSION = "canonical_compression"
    PROCESS_TRANSPORT = "process_transport"
    ARTIFACT_PUBLICATION = "artifact_publication"
    CHART_PROJECTION = "chart_projection"
    RESULT_WIDGET_RENDER_ACKNOWLEDGEMENT = "result_widget_render_acknowledgement"


class AnalysisTimingStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARKED = "parked"


def _require_non_empty_str(value: object, name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact str")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _require_non_negative_int(value: object, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact int")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class AnalysisStageTimingEvent:
    """One immutable event containing only identity and bounded scalar data."""

    run_id: str
    analysis_attempt_id: str
    boundary: AnalysisTimingBoundary
    started_ns: int
    finished_ns: int
    duration_ns: int
    byte_count: int
    status: AnalysisTimingStatus

    def __post_init__(self) -> None:
        _require_non_empty_str(self.run_id, "run_id")
        _require_non_empty_str(self.analysis_attempt_id, "analysis_attempt_id")
        if type(self.boundary) is not AnalysisTimingBoundary:
            raise TypeError("boundary must be an exact AnalysisTimingBoundary")
        _require_non_negative_int(self.started_ns, "started_ns")
        _require_non_negative_int(self.finished_ns, "finished_ns")
        _require_non_negative_int(self.duration_ns, "duration_ns")
        _require_non_negative_int(self.byte_count, "byte_count")
        if self.finished_ns < self.started_ns:
            raise ValueError("finished_ns must not precede started_ns")
        if self.duration_ns != self.finished_ns - self.started_ns:
            raise ValueError("duration_ns must equal finished_ns - started_ns")
        if type(self.status) is not AnalysisTimingStatus:
            raise TypeError("status must be an exact AnalysisTimingStatus")


@dataclass(frozen=True, slots=True)
class AnalysisStageTimings:
    """Closed immutable collection with at most one event per boundary."""

    events: tuple[AnalysisStageTimingEvent, ...]

    def __post_init__(self) -> None:
        if type(self.events) is not tuple:
            raise TypeError("events must be an exact tuple")
        seen: set[AnalysisTimingBoundary] = set()
        identity: tuple[str, str] | None = None
        for event in self.events:
            if type(event) is not AnalysisStageTimingEvent:
                raise TypeError("events must contain exact AnalysisStageTimingEvent values")
            if event.boundary in seen:
                raise ValueError(f"duplicate timing boundary: {event.boundary.value}")
            seen.add(event.boundary)
            event_identity = (event.run_id, event.analysis_attempt_id)
            if identity is None:
                identity = event_identity
            elif event_identity != identity:
                raise ValueError("all timing events must use the same run and attempt ids")


class StageTimer:
    """Measure named stages using an injected nanosecond monotonic clock."""

    __slots__ = (
        "_analysis_attempt_id",
        "_events",
        "_finished_boundaries",
        "_perf_counter_ns",
        "_run_id",
        "_started",
    )

    def __init__(
        self,
        *,
        identity: object,
        perf_counter_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        # The local import avoids coupling the pure execution contracts to the
        # mutable timer implementation during module import.
        from .staged_execution import AnalysisBundleIdentity

        if type(identity) is not AnalysisBundleIdentity:
            raise TypeError("identity must be an exact AnalysisBundleIdentity")
        if not callable(perf_counter_ns):
            raise TypeError("perf_counter_ns must be callable")
        self._run_id = identity.run_id
        self._analysis_attempt_id = identity.analysis_attempt_id
        self._perf_counter_ns = perf_counter_ns
        self._started: dict[AnalysisTimingBoundary, int] = {}
        self._finished_boundaries: set[AnalysisTimingBoundary] = set()
        self._events: list[AnalysisStageTimingEvent] = []

    def start(self, boundary: AnalysisTimingBoundary) -> None:
        self._require_boundary(boundary)
        if boundary in self._started or boundary in self._finished_boundaries:
            raise ValueError(f"duplicate timing boundary: {boundary.value}")
        self._started[boundary] = self._read_clock()

    def finish(
        self,
        boundary: AnalysisTimingBoundary,
        *,
        byte_count: int = 0,
        status: AnalysisTimingStatus = AnalysisTimingStatus.SUCCEEDED,
    ) -> AnalysisStageTimingEvent:
        self._require_boundary(boundary)
        if boundary not in self._started:
            if boundary in self._finished_boundaries:
                raise ValueError(f"duplicate timing boundary: {boundary.value}")
            raise ValueError(f"timing boundary was not started: {boundary.value}")
        _require_non_negative_int(byte_count, "byte_count")
        if type(status) is not AnalysisTimingStatus:
            raise TypeError("status must be an exact AnalysisTimingStatus")
        started_ns = self._started[boundary]
        finished_ns = self._read_clock()
        duration_ns = finished_ns - started_ns
        if duration_ns < 0:
            raise ValueError("timing clock produced a negative delta")
        event = AnalysisStageTimingEvent(
            run_id=self._run_id,
            analysis_attempt_id=self._analysis_attempt_id,
            boundary=boundary,
            started_ns=started_ns,
            finished_ns=finished_ns,
            duration_ns=duration_ns,
            byte_count=byte_count,
            status=status,
        )
        del self._started[boundary]
        self._finished_boundaries.add(boundary)
        self._events.append(event)
        return event

    def snapshot(self) -> AnalysisStageTimings:
        if self._started:
            raise ValueError("cannot snapshot timings with unfinished boundaries")
        return AnalysisStageTimings(tuple(self._events))

    @staticmethod
    def _require_boundary(boundary: object) -> None:
        if type(boundary) is not AnalysisTimingBoundary:
            raise TypeError("boundary must be an exact AnalysisTimingBoundary")

    def _read_clock(self) -> int:
        value = self._perf_counter_ns()
        _require_non_negative_int(value, "perf_counter_ns result")
        return value


__all__ = (
    "BENCHMARK_DURATION_FIELDS",
    "BENCHMARK_SIZE_FIELDS",
    "BENCHMARK_TIMESTAMP_FIELDS",
    "AnalysisStageTimingEvent",
    "AnalysisStageTimings",
    "AnalysisTimingBoundary",
    "AnalysisTimingStatus",
    "StageTimer",
    "benchmark_percentiles",
    "validate_benchmark_report",
)
