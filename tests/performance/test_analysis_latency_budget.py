from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from app.service.analysis.performance_diagnostics import (
    BENCHMARK_DURATION_FIELDS,
    BENCHMARK_SIZE_FIELDS,
    BENCHMARK_TIMESTAMP_FIELDS,
    benchmark_percentiles,
    validate_benchmark_report,
)
from scripts.benchmark_two_stage_analysis import (
    NAMED_SLA_CPU_FRAGMENT,
    NAMED_SLA_LOGICAL_PROCESSORS,
    NAMED_SLA_MIN_MEMORY_BYTES,
    evaluate_sla_host,
    format_console_summary,
    _freeze_new_capture_epoch,
    _matching_descendant_pids,
    _run_six_overlap,
    write_json_report,
)


pytestmark = pytest.mark.performance


def _iteration(index: int, *, decision_ns: int = 400_000_000) -> dict:
    frozen = 1_000_000_000 + index * 2_000_000_000
    timestamps = {
        field: frozen + offset * 1_000_000
        for offset, field in enumerate(BENCHMARK_TIMESTAMP_FIELDS)
    }
    timestamps["audio_frozen_ns"] = frozen
    timestamps["plc_acknowledged_ns"] = frozen + decision_ns - 2_000_000
    timestamps["decision_painted_ns"] = frozen + decision_ns
    timestamps["charts_painted_ns"] = frozen + 1_200_000_000
    timestamps["artifact_fsync_finished_ns"] = frozen + 1_100_000_000
    durations = {
        field: 1_000_000 + index
        for field in BENCHMARK_DURATION_FIELDS
    }
    durations["decision_endpoint_ns"] = decision_ns
    durations["charts_endpoint_ns"] = 1_200_000_000
    durations["quiet_fence_ns"] = 250_000_000
    sizes = {field: 1024 + index for field in BENCHMARK_SIZE_FIELDS}
    return {
        "iteration": index,
        "identity": {
            "run_id": f"run-{index}",
            "analysis_attempt_id": f"attempt-{index}",
        },
        "timestamps_ns": timestamps,
        "durations_ns": durations,
        "sizes_bytes": sizes,
        "steady_ai_process_spawned": False,
        "parity": {
            "judgment_equal": True,
            "limits_equal": True,
            "numeric_within_tolerance": True,
        },
    }


def _host(*, qt_platform: str = "windows") -> dict:
    return {
        "hostname": "BENCH-01",
        "os": "Windows",
        "os_release": "11",
        "architecture": "AMD64",
        "processor": f"{NAMED_SLA_CPU_FRAGMENT} with Radeon Graphics",
        "cpu_physical_cores": 6,
        "cpu_logical_processors": NAMED_SLA_LOGICAL_PROCESSORS,
        "cpu_max_clock_mhz": 3600,
        "total_memory_bytes": NAMED_SLA_MIN_MEMORY_BYTES + 1024,
        "disk_model": "NVMe Test Disk",
        "disk_media_type": "SSD",
        "python_version": "3.12.3",
        "python_executable": "C:/Python/python.exe",
        "dependencies": {"numpy": "2.4.2", "PyQt6": "6.10.2"},
        "power_mode": "Ultimate Performance",
        "qt_platform": qt_platform,
        "interactive_desktop": qt_platform == "windows",
        "result_window_shown": qt_platform == "windows",
        "result_window_minimized": False,
    }


def _scenario(name: str = "single_channel") -> dict:
    iterations = [_iteration(index) for index in range(30)]
    return {
        "name": name,
        "status": "MEASURED",
        "input": {
            "source": "production_wav",
            "path": "C:/data/input.wav",
            "sha256": "a" * 64,
            "duration_seconds": 8.85,
            "sample_rate_hz": 44_100,
            "channel_count": 1,
            "frame_count": 390_285,
            "dtype": "int16",
        },
        "config": {
            "config_id": 1,
            "config_sha256": "b" * 64,
            "algorithm_revision": "revision-1",
        },
        "toolchain": {
            "toolchain_id": "ai-1",
            "execution_revision": "rev-1",
            "entrypoint_sha256": "c" * 64,
        },
        "legacy_oracle": {"status": "PASS"},
        "iterations": iterations,
        "summary": {
            field: benchmark_percentiles(
                [item["durations_ns"][field] for item in iterations]
            )
            for field in BENCHMARK_DURATION_FIELDS
        },
    }


def _report() -> dict:
    return {
        "schema": "analysis-latency-benchmark/v1",
        "generated_at_utc": "2026-09-01T00:00:00.000000Z",
        "host": _host(),
        "endpoints": {
            "plc": {
                "evaluated": True,
                "source": "PlcResultAttemptReport.BOUNDARY_RETURNED",
            },
            "decision_paint": {
                "evaluated": True,
                "source": "ResultModule.decisionPainted",
            },
            "chart_paint": {
                "evaluated": True,
                "source": "shown chart widget paintEvent",
            },
            "durable_db": {
                "evaluated": True,
                "source": "AnalysisPersistenceBoundary.accept_enrichment/EnrichmentCommittedEvent",
            },
        },
        "scenarios": {
            "single_channel": _scenario(),
            "six_run_overlap": {
                **_scenario("six_run_overlap"),
                "iterations": [_iteration(index) for index in range(6)],
            },
            "two_channel_48khz": _scenario("two_channel_48khz"),
        },
        "sla": {
            "status": "PASS",
            "evaluated": True,
            "blockers": [],
            "decision_limit_ns": 500_000_000,
            "charts_limit_ns": 1_500_000_000,
        },
    }


def test_percentiles_are_nearest_rank_and_require_real_nonnegative_samples():
    values = list(range(1, 101))
    assert benchmark_percentiles(values) == {
        "sample_count": 100,
        "p50_ns": 50,
        "p95_ns": 95,
        "max_ns": 100,
    }
    with pytest.raises(ValueError, match="at least one"):
        benchmark_percentiles([])
    with pytest.raises(TypeError, match="exact int"):
        benchmark_percentiles([1.0])
    with pytest.raises(ValueError, match="non-negative"):
        benchmark_percentiles([-1])


def test_report_contract_requires_all_stage_fields_and_thirty_warm_samples():
    report = _report()
    validate_benchmark_report(report, require_hard_sla=True)

    missing_stage = _report()
    del missing_stage["scenarios"]["single_channel"]["iterations"][0][
        "timestamps_ns"
    ]["durable_db_committed_ns"]
    with pytest.raises(ValueError, match="durable_db_committed_ns"):
        validate_benchmark_report(missing_stage, require_hard_sla=True)

    too_short = _report()
    too_short["scenarios"]["single_channel"]["iterations"] = too_short[
        "scenarios"
    ]["single_channel"]["iterations"][:29]
    with pytest.raises(ValueError, match="at least 30"):
        validate_benchmark_report(too_short, require_hard_sla=True)


def test_report_rejects_chart_paths_without_a_fresh_250ms_quiet_fence():
    report = _report()
    report["scenarios"]["single_channel"]["iterations"][0]["durations_ns"][
        "quiet_fence_ns"
    ] = 249_999_999

    with pytest.raises(ValueError, match="250 ms quiet fence"):
        validate_benchmark_report(report, require_hard_sla=True)


def test_hard_report_rejects_fabricated_or_signal_only_plc_and_qt_endpoints():
    report = _report()
    report["endpoints"]["plc"]["source"] = "write_test_result_sign.emit"
    with pytest.raises(ValueError, match="actual PLC"):
        validate_benchmark_report(report, require_hard_sla=True)

    report = _report()
    report["endpoints"]["decision_paint"]["source"] = "dashboard signal"
    with pytest.raises(ValueError, match="ResultModule.decisionPainted"):
        validate_benchmark_report(report, require_hard_sla=True)

    report = _report()
    report["scenarios"]["single_channel"]["legacy_oracle"] = {
        "status": "NOT_EVALUATED"
    }
    with pytest.raises(ValueError, match="independent legacy oracle"):
        validate_benchmark_report(report, require_hard_sla=True)

    report = _report()
    report["endpoints"]["durable_db"] = {
        "evaluated": True,
        "source": "diagnostic commit probe",
    }
    with pytest.raises(ValueError, match="production persistence"):
        validate_benchmark_report(report, require_hard_sla=True)


def test_hidden_or_offscreen_qt_is_functional_only_and_not_sla_eligible():
    host = _host(qt_platform="offscreen")
    eligibility = evaluate_sla_host(host)
    assert eligibility["eligible"] is False
    assert "offscreen" in " ".join(eligibility["blockers"]).lower()

    report = _report()
    report["host"] = host
    report["sla"] = {
        **report["sla"],
        "status": "SLA_NOT_EVALUATED",
        "evaluated": False,
        "blockers": eligibility["blockers"],
    }
    validate_benchmark_report(report, require_hard_sla=False)


def test_named_sla_host_validation_checks_machine_shape_and_shown_window():
    assert evaluate_sla_host(_host()) == {"eligible": True, "blockers": []}

    low_memory = _host()
    low_memory["total_memory_bytes"] = 8 * 1024**3
    result = evaluate_sla_host(low_memory)
    assert result["eligible"] is False
    assert "memory" in " ".join(result["blockers"]).lower()

    minimized = _host()
    minimized["result_window_minimized"] = True
    result = evaluate_sla_host(minimized)
    assert result["eligible"] is False
    assert "minimized" in " ".join(result["blockers"]).lower()

    for field, invalid, fragment in (
        ("cpu_physical_cores", 4, "physical"),
        ("cpu_max_clock_mhz", 4200, "clock"),
        ("os_release", "10", "windows 11"),
        ("disk_model", "Generic SATA Disk", "nvme"),
    ):
        candidate = _host()
        candidate[field] = invalid
        result = evaluate_sla_host(candidate)
        assert result["eligible"] is False
        assert fragment in " ".join(result["blockers"]).lower()

    generic_media_real_nvme_model = _host()
    generic_media_real_nvme_model["disk_media_type"] = "Fixed hard disk media"
    assert evaluate_sla_host(generic_media_real_nvme_model)["eligible"] is True


def test_json_writer_is_atomic_and_console_summary_is_concise(tmp_path: Path):
    report = _report()
    output = tmp_path / "nested" / "analysis-latency.json"
    write_json_report(output, report)
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert not output.with_suffix(output.suffix + ".tmp").exists()

    summary = format_console_summary(report)
    assert "PASS" in summary
    assert "single_channel" in summary
    assert "p50=" in summary and "p95=" in summary and "max=" in summary
    assert len(summary.splitlines()) <= 8


def test_console_exposes_measured_paint_and_chart_overrun_without_plc():
    report = _report()
    report["sla"] = {
        **report["sla"],
        "status": "SLA_NOT_EVALUATED",
        "evaluated": False,
        "blockers": ["live PLC disabled"],
    }
    report["endpoints"]["plc"] = {"evaluated": False, "source": "not enabled"}
    single = report["scenarios"]["single_channel"]
    for iteration in single["iterations"]:
        iteration["timestamps_ns"]["plc_acknowledged_ns"] = None
        iteration["durations_ns"]["plc_ns"] = None
        iteration["durations_ns"]["decision_endpoint_ns"] = None
        iteration["durations_ns"]["charts_endpoint_ns"] = 1_650_000_000
    single["summary"].pop("decision_endpoint_ns")
    single["summary"]["charts_endpoint_ns"] = benchmark_percentiles(
        [1_650_000_000] * 30
    )
    for name in ("six_run_overlap", "two_channel_48khz"):
        report["scenarios"][name] = {
            "name": name,
            "status": "BLOCKED",
            "iterations": [],
            "summary": {},
        }

    summary = format_console_summary(report)

    assert "SLA_NOT_EVALUATED" in summary
    assert "decision_visible" in summary
    assert "p50=400.0ms" in summary
    assert "target<=500.0ms PASS" in summary
    assert "charts" in summary
    assert "p95=1650.0ms" in summary
    assert "target<=1500.0ms MISS" in summary
    assert "decision_authoritative" not in summary
    assert "stages:" in summary
    assert "live PLC disabled" in summary


def test_six_run_overlap_finishes_only_after_all_decisions_freeze():
    capture_active = False
    events = []

    def set_capture_active(value):
        nonlocal capture_active
        capture_active = value
        events.append(("capture", value))

    def prepare(index):
        return {"index": index, "frozen_ns": (index + 1) * 10}

    def preregister(prepared):
        events.append(("preregister", prepared["index"]))

    def begin(prepared, on_frozen):
        assert capture_active is True
        index = prepared["index"]
        events.append(("decision", index))
        on_frozen(prepared["frozen_ns"])
        events.append(("decision_complete", index))
        return prepared

    def finish(pending, quiet_origin_ns):
        assert capture_active is False
        events.append(("enrichment", pending["index"], quiet_origin_ns))
        return pending["index"]

    results, snapshot = _run_six_overlap(
        set_capture_active=set_capture_active,
        prepare_iteration=prepare,
        preregister_iteration=preregister,
        begin_iteration=begin,
        complete_decision=lambda pending: pending,
        finish_iteration=finish,
    )

    assert results == list(range(6))
    assert snapshot == {
        "preregistered_attempts": 6,
        "inflight_decisions_at_final_freeze": 1,
        "final_freeze_ns": 60,
    }
    assert events[:6] == [("preregister", index) for index in range(6)]
    final_freeze_position = max(
        events.index(("decision", index)) for index in range(6)
    )
    capture_release_position = events.index(("capture", False))
    assert capture_release_position > final_freeze_position
    assert capture_release_position < max(
        events.index(("decision_complete", index)) for index in range(6)
    )
    assert events[-6:] == [
        ("enrichment", index, 60) for index in range(6)
    ]


def test_each_sequential_iteration_opens_a_fresh_capture_epoch():
    active = False
    now_ns = 1_000_000_000
    events = []

    def set_active(value):
        nonlocal active
        active = value
        events.append(("set", value))

    def observe():
        events.append(("observe", active))

    def clock():
        nonlocal now_ns
        value = now_ns
        now_ns += 1_000_000_000
        return value

    freezes = [
        _freeze_new_capture_epoch(
            set_capture_active=set_active,
            observe_capture_state=observe,
            perf_counter_ns=clock,
        )
        for _index in range(2)
    ]

    assert freezes == [1_000_000_000, 2_000_000_000]
    assert events == [
        ("set", True),
        ("observe", True),
        ("set", False),
        ("observe", False),
        ("set", True),
        ("observe", True),
        ("set", False),
        ("observe", False),
    ]


def test_ai_process_snapshot_only_matches_recursive_descendants():
    entries = (
        (10, 1, "python.exe"),
        (11, 10, "analysis-child.exe"),
        (12, 11, "demo_runner.EXE"),
        (13, 99, "demo_runner.exe"),
    )

    assert _matching_descendant_pids(entries, 10, "demo_runner.exe") == {12}


def test_production_stages_publish_real_internal_decision_and_enrichment_timings(
    tmp_path: Path,
):
    from app.service.analysis.performance_diagnostics import AnalysisTimingBoundary
    from app.service.analysis.production_executor import ProductionAnalysisExecutor
    from tests.analysis_scheduler.test_staged_production_executor import (
        _Audio,
        _Token,
        _job,
    )

    audio = _Audio(sample_rate=44_100, channel_count=1)
    executor = ProductionAnalysisExecutor()
    decision = executor.execute_decision(_job(tmp_path, audio), audio, _Token())
    decision_boundaries = {event.boundary for event in decision.timings.events}
    assert {
        AnalysisTimingBoundary.LEGACY_DECISION_DSP,
        AnalysisTimingBoundary.DECISION_FEATURE_EXTRACTION,
        AnalysisTimingBoundary.DECISION_JUDGMENT,
        AnalysisTimingBoundary.RESIDENT_AI_RESPONSE,
        AnalysisTimingBoundary.DECISION_PROJECTION,
    }.issubset(decision_boundaries)
    assert all(event.finished_ns >= event.started_ns for event in decision.timings.events)

    enrichment = executor.execute_enrichment(decision.bundle_token)
    enrichment_by_boundary = {
        event.boundary: event for event in enrichment.timings.events
    }
    assert {
        AnalysisTimingBoundary.ENRICHMENT_DSP,
        AnalysisTimingBoundary.CANONICAL_ENCODING,
        AnalysisTimingBoundary.CANONICAL_COMPRESSION,
    }.issubset(enrichment_by_boundary)
    assert enrichment_by_boundary[
        AnalysisTimingBoundary.CANONICAL_ENCODING
    ].byte_count == len(enrichment.canonical_result_json.encode("utf-8"))
    assert enrichment_by_boundary[
        AnalysisTimingBoundary.CANONICAL_COMPRESSION
    ].byte_count == len(enrichment.result_payload_zlib)


def test_decision_feature_metrics_overlap_without_changing_metric_results(monkeypatch):
    import numpy as np

    from app.service.analysis import dsp
    from app.service.analysis.production_executor import ProductionAnalysisExecutor

    active = 0
    max_active = 0
    lock = threading.Lock()

    def measured(value):
        def run(*_args, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return value

        return run

    monkeypatch.setattr(dsp, "apply_mic_calibration", lambda value, _s: value)
    monkeypatch.setattr(dsp, "compute_spl_trace", measured(([0.1], [61.0])))
    monkeypatch.setattr(dsp, "compute_spl_leq", measured(60.0))
    monkeypatch.setattr(
        dsp, "compute_loudness_trace_for_window", measured(([0.0], [1.5], 1.5))
    )
    monkeypatch.setattr(
        dsp, "compute_loudness_trace", measured(([0.0], [1.5], 1.5))
    )
    monkeypatch.setattr(
        dsp,
        "compute_fft_spectrum",
        measured((np.array([0.0]), np.array([-10.0]))),
    )
    monkeypatch.setattr(
        dsp,
        "compute_stft",
        measured((np.array([0.0]), np.array([0.0]), np.array([[-20.0]]))),
    )
    monkeypatch.setattr(
        dsp,
        "compute_octave_bands",
        measured((np.array([1000.0]), np.array([-30.0]))),
    )

    features = ProductionAnalysisExecutor()._build_channel_features(
        samples=np.ones((100, 1), dtype=np.float64),
        sample_rate=100.0,
        config={
            "selected_channels": [0],
            "channels": [{"channel_index": 0, "spl_weighting": "Z"}],
        },
        calibration={"In1": {"deviation_value": "0"}},
        mapping={0: 0},
        start_sec=0.1,
        end_sec=0.9,
    )

    assert max_active >= 2
    assert features[0].spl_values.tolist() == [61.0]
    assert features[0].loudness_values.tolist() == [1.5]
    assert features[0].fft_magnitudes.tolist() == pytest.approx(
        [-10.0 - 20.0 * np.log10(20e-6)]
    )


def test_validated_sla_report_from_environment():
    """Hard PASS is only asserted for a fresh, explicitly supplied host report."""

    import os

    path_text = os.environ.get("ANALYSIS_SLA_REPORT")
    if not path_text:
        host = _host(qt_platform="not-running")
        pytest.skip(
            "ANALYSIS_SLA_REPORT not supplied; SLA_NOT_EVALUATED; "
            f"fingerprint={json.dumps(host, sort_keys=True)}"
        )
    report = json.loads(Path(path_text).read_text(encoding="utf-8"))
    validate_benchmark_report(report, require_hard_sla=True)
    assert report["sla"]["status"] == "PASS"
