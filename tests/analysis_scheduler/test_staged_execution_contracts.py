from __future__ import annotations

import copy
import json
import math
import zlib
from dataclasses import FrozenInstanceError, fields, replace

import pytest

from app.service.analysis import staged_execution, staged_result_payload
from app.service.analysis.performance_diagnostics import (
    AnalysisStageTimingEvent,
    AnalysisStageTimings,
    AnalysisTimingBoundary,
    AnalysisTimingStatus,
    StageTimer,
)
from app.service.analysis.staged_execution import (
    AnalysisBundleBudget,
    AnalysisBundleIdentity,
    AnalysisBundleToken,
    AnalysisDecisionEnvelope,
    AnalysisEnrichmentReport,
    AnalysisPhase,
)
from app.service.analysis.staged_result_payload import (
    REQUIRED_RESULT_FIELDS,
    build_lightweight_canonical_result,
    validate_enrichment_compatibility,
)


def make_identity(**changes: object) -> AnalysisBundleIdentity:
    values: dict[str, object] = {
        "run_id": "run-1",
        "analysis_attempt_id": "attempt-1",
        "content_revision": "sha256:one",
        "algorithm_revision": "algorithm-1",
        "config_identity": "config-1",
        "calibration_identity": "calibration-1",
        "channel_mapping_identity": "channels-0",
        "window_start_seconds": 0.0,
        "window_end_seconds": 8.85,
    }
    values.update(changes)
    return AnalysisBundleIdentity(**values)


def make_result(*, judgment: str = "PASS") -> dict[str, object]:
    summary = {
        "overall_status": "pass" if judgment == "PASS" else "fail",
        "overall_label": "ok" if judgment == "PASS" else "not ok",
        "rows": [],
    }
    return {
        "legacy_overall": "qualified" if judgment == "PASS" else "unqualified",
        "limit_pass": judgment == "PASS",
        "ai_pass": None,
        "ai_score": None,
        "analysis_window": [0.0, 8.85],
        "channel_results": [
            {
                "channel_index": 0,
                "spl_pass": judgment == "PASS",
                "fft_available": True,
            }
        ],
        "review": {"algorithm_revision": "algorithm-1"},
        "displayed_config_id": 1,
        "config_payload": {"selected_channels": [0]},
        "captured_duration_sec": 8.85,
        "default_window": {"start_sec": 0.0, "end_sec": 8.85},
        "fallback_window": {"start_sec": 0.0, "end_sec": 8.85},
        "manual_reanalysis_allowed": True,
        "judgment_payload": {
            "overall": judgment,
            "channel_results": [{"channel_index": 0, "spl_pass": judgment == "PASS"}],
        },
        "summary_payload": summary,
        "chart_payload": {"0": {"spl": {"values": [1.0]}}},
    }


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compressed_json(value: str) -> bytes:
    return zlib.compress(value.encode("utf-8"))


def make_timing_event(
    boundary: AnalysisTimingBoundary = AnalysisTimingBoundary.AUDIO_FROZEN,
    **changes: object,
) -> AnalysisStageTimingEvent:
    values: dict[str, object] = {
        "run_id": "run-1",
        "analysis_attempt_id": "attempt-1",
        "boundary": boundary,
        "started_ns": 10,
        "finished_ns": 20,
        "duration_ns": 10,
        "byte_count": 0,
        "status": AnalysisTimingStatus.SUCCEEDED,
    }
    values.update(changes)
    return AnalysisStageTimingEvent(**values)


@pytest.mark.parametrize(
    "field_name",
    [
        "run_id",
        "analysis_attempt_id",
        "content_revision",
        "algorithm_revision",
        "config_identity",
        "calibration_identity",
        "channel_mapping_identity",
    ],
)
def test_bundle_identity_requires_exact_non_empty_strings(field_name: str):
    with pytest.raises(ValueError, match=field_name):
        make_identity(**{field_name: ""})
    with pytest.raises(TypeError, match=field_name):
        make_identity(**{field_name: 1})


@pytest.mark.parametrize("value", [-1.0, math.inf, -math.inf, math.nan])
def test_bundle_identity_rejects_non_finite_or_negative_windows(value: float):
    with pytest.raises(ValueError, match="window_start_seconds"):
        make_identity(window_start_seconds=value)


def test_bundle_identity_requires_a_positive_window_and_exact_floats():
    with pytest.raises(ValueError, match="after"):
        make_identity(window_start_seconds=1.0, window_end_seconds=1.0)
    with pytest.raises(TypeError, match="window_start_seconds"):
        make_identity(window_start_seconds=0)


def test_bundle_identity_rejects_wrong_content_revision():
    identity = make_identity(content_revision="sha256:one")
    token = AnalysisBundleToken(slot_id=1, bundle_id="bundle-1", identity=identity)
    with pytest.raises(ValueError, match="content revision"):
        token.require_identity(replace(identity, content_revision="sha256:two"))


def test_bundle_token_rejects_every_identity_mismatch_and_is_immutable():
    identity = make_identity()
    token = AnalysisBundleToken(slot_id=1, bundle_id="bundle-1", identity=identity)
    mismatches = {
        "run_id": "run-2",
        "analysis_attempt_id": "attempt-2",
        "algorithm_revision": "algorithm-2",
        "config_identity": "config-2",
        "calibration_identity": "calibration-2",
        "channel_mapping_identity": "channels-1",
        "window_end_seconds": 9.0,
    }
    for field_name, value in mismatches.items():
        with pytest.raises(ValueError, match=field_name.replace("_", " ")):
            token.require_identity(replace(identity, **{field_name: value}))
    with pytest.raises(FrozenInstanceError):
        token.bundle_id = "forged"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "exception"),
    [
        ({"slot_id": True}, TypeError),
        ({"slot_id": -1}, ValueError),
        ({"bundle_id": 1}, TypeError),
        ({"bundle_id": ""}, ValueError),
        ({"identity": object()}, TypeError),
    ],
)
def test_bundle_token_is_closed(changes: dict[str, object], exception: type[Exception]):
    values = {"slot_id": 0, "bundle_id": "bundle-1", "identity": make_identity()}
    values.update(changes)
    with pytest.raises(exception):
        AnalysisBundleToken(**values)


def test_bundle_budget_is_four_audio_copies_plus_32_mib():
    budget = AnalysisBundleBudget(frame_count=390_285, channel_count=1, dtype_size=4)
    assert budget.audio_bytes == 390_285 * 4
    assert budget.max_bytes == 4 * 390_285 * 4 + 32 * 1024 * 1024
    assert budget.require_within(budget.max_bytes) is None
    with pytest.raises(ValueError, match="exceeds"):
        budget.require_within(budget.max_bytes + 1)


@pytest.mark.parametrize("field_name", ["frame_count", "channel_count", "dtype_size"])
def test_bundle_budget_requires_exact_positive_dimensions(field_name: str):
    values = {"frame_count": 1, "channel_count": 1, "dtype_size": 4}
    for hostile in (0, -1, True, 1.0):
        values[field_name] = hostile
        with pytest.raises((TypeError, ValueError), match=field_name):
            AnalysisBundleBudget(**values)


def test_stage_timings_require_an_immutable_tuple_without_duplicate_boundaries():
    event = make_timing_event()
    timings = AnalysisStageTimings(events=(event,))
    assert type(timings.events) is tuple
    with pytest.raises(TypeError, match="tuple"):
        AnalysisStageTimings(events=[event])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicate"):
        AnalysisStageTimings(events=(event, event))
    with pytest.raises(FrozenInstanceError):
        timings.events = ()  # type: ignore[misc]


def test_stage_timing_event_rejects_unknown_stage_names_and_invalid_timings():
    event_values = {
        "run_id": "run-1",
        "analysis_attempt_id": "attempt-1",
        "boundary": AnalysisTimingBoundary.AUDIO_FROZEN,
        "started_ns": 10,
        "finished_ns": 20,
        "duration_ns": 10,
        "byte_count": 0,
        "status": AnalysisTimingStatus.SUCCEEDED,
    }
    for mutation, exception in (
        ({"boundary": "audio_frozen"}, TypeError),
        ({"started_ns": -1}, ValueError),
        ({"finished_ns": 9}, ValueError),
        ({"duration_ns": 11}, ValueError),
        ({"byte_count": -1}, ValueError),
        ({"duration_ns": 10.0}, TypeError),
        ({"status": "succeeded"}, TypeError),
    ):
        with pytest.raises(exception):
            AnalysisStageTimingEvent(**(event_values | mutation))


def test_stage_timer_uses_the_injected_clock_and_emits_one_bounded_event():
    ticks = iter((100, 125))
    timer = StageTimer(identity=make_identity(), perf_counter_ns=lambda: next(ticks))
    timer.start(AnalysisTimingBoundary.AUDIO_TRANSPORT_READY)
    event = timer.finish(
        AnalysisTimingBoundary.AUDIO_TRANSPORT_READY,
        byte_count=64,
    )
    assert event.duration_ns == 25
    assert timer.snapshot().events == (event,)
    assert {item.name for item in fields(event)} == {
        "run_id",
        "analysis_attempt_id",
        "boundary",
        "started_ns",
        "finished_ns",
        "duration_ns",
        "byte_count",
        "status",
    }
    with pytest.raises(ValueError, match="duplicate"):
        timer.start(AnalysisTimingBoundary.AUDIO_TRANSPORT_READY)


def test_stage_timer_rejects_unknown_boundaries_and_clock_regression():
    timer = StageTimer(identity=make_identity(), perf_counter_ns=iter((20, 10)).__next__)
    with pytest.raises(TypeError, match="boundary"):
        timer.start("audio_frozen")  # type: ignore[arg-type]
    timer.start(AnalysisTimingBoundary.AUDIO_FROZEN)
    with pytest.raises(ValueError, match="negative"):
        timer.finish(AnalysisTimingBoundary.AUDIO_FROZEN)


def test_lightweight_payload_keeps_exact_compatible_root_and_result_fields():
    canonical = build_lightweight_canonical_result(
        judgment="PASS",
        result=make_result(),
    )
    payload = json.loads(canonical)
    assert list(payload) == ["judgment", "result"]
    assert set(payload["result"]) == set(REQUIRED_RESULT_FIELDS)
    assert payload["result"]["chart_payload"] == {}
    assert canonical == json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def test_lightweight_payload_rejects_missing_or_unknown_compatibility_fields():
    result = make_result()
    result.pop("summary_payload")
    with pytest.raises(ValueError, match="result fields"):
        build_lightweight_canonical_result(judgment="PASS", result=result)
    result = make_result()
    result["unknown"] = True
    with pytest.raises(ValueError, match="result fields"):
        build_lightweight_canonical_result(judgment="PASS", result=result)


def test_enrichment_validator_accepts_only_chart_materialization_changes():
    identity = make_identity()
    decision_json = build_lightweight_canonical_result(
        judgment="PASS",
        result=make_result(),
    )
    enriched_result = make_result()
    enriched_json = canonical_json(
        {"judgment": "PASS", "result": enriched_result}
    )
    assert (
        validate_enrichment_compatibility(
            decision_identity=identity,
            enrichment_identity=identity,
            decision_canonical_result_json=decision_json,
            enrichment_canonical_result_json=enriched_json,
        )
        is None
    )


def test_enrichment_validator_rejects_judgment_changes():
    identity = make_identity()
    decision_json = build_lightweight_canonical_result(
        judgment="PASS",
        result=make_result(),
    )
    enriched = make_result()
    enriched_json = canonical_json({"judgment": "FAIL", "result": enriched})
    with pytest.raises(ValueError, match="judgment"):
        validate_enrichment_compatibility(
            decision_identity=identity,
            enrichment_identity=identity,
            decision_canonical_result_json=decision_json,
            enrichment_canonical_result_json=enriched_json,
        )


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("legacy_overall", "changed"),
        ("limit_pass", 1),
        ("ai_pass", True),
        ("ai_score", 0.75),
        ("judgment_payload", {"overall": "changed", "channel_results": []}),
        (
            "summary_payload",
            {"overall_status": "pass", "overall_label": "changed", "rows": []},
        ),
        ("channel_results", [{"channel_index": 0, "spl_pass": False}]),
        ("review", {"algorithm_revision": "changed"}),
        ("displayed_config_id", 2),
        ("config_payload", {"selected_channels": [1]}),
        ("analysis_window", [0.25, 8.85]),
    ],
)
def test_enrichment_validator_rejects_every_non_chart_change(
    field_name: str,
    changed_value: object,
):
    identity = make_identity()
    decision_json = build_lightweight_canonical_result(
        judgment="PASS",
        result=make_result(),
    )
    enriched = copy.deepcopy(make_result())
    enriched[field_name] = changed_value
    enriched_json = canonical_json({"judgment": "PASS", "result": enriched})
    with pytest.raises(ValueError, match="non-chart"):
        validate_enrichment_compatibility(
            decision_identity=identity,
            enrichment_identity=identity,
            decision_canonical_result_json=decision_json,
            enrichment_canonical_result_json=enriched_json,
        )


@pytest.mark.parametrize(
    ("identity_change", "message"),
    [
        ({"run_id": "run-2"}, "run id"),
        ({"analysis_attempt_id": "attempt-2"}, "analysis attempt id"),
        ({"content_revision": "sha256:two"}, "content revision"),
        ({"algorithm_revision": "algorithm-2"}, "algorithm revision"),
        ({"config_identity": "config-2"}, "config identity"),
        ({"calibration_identity": "calibration-2"}, "calibration identity"),
        ({"channel_mapping_identity": "channels-1"}, "channel mapping identity"),
        ({"window_start_seconds": 0.25}, "window start seconds"),
    ],
)
def test_enrichment_validator_rejects_identity_changes(identity_change, message):
    identity = make_identity()
    decision_json = build_lightweight_canonical_result(
        judgment="PASS", result=make_result()
    )
    enriched = make_result()
    enriched_json = canonical_json({"judgment": "PASS", "result": enriched})
    with pytest.raises(ValueError, match=message):
        validate_enrichment_compatibility(
            decision_identity=identity,
            enrichment_identity=replace(identity, **identity_change),
            decision_canonical_result_json=decision_json,
            enrichment_canonical_result_json=enriched_json,
        )


def test_decision_and_enrichment_reports_require_exact_closed_contracts():
    identity = make_identity()
    token = AnalysisBundleToken(0, "bundle-1", identity)
    timings = AnalysisStageTimings(())
    decision_json = build_lightweight_canonical_result(
        judgment="PASS", result=make_result()
    )
    decision = AnalysisDecisionEnvelope(
        identity=identity,
        judgment="PASS",
        canonical_result_json=decision_json,
        summary_payload_json=canonical_json(make_result()["summary_payload"]),
        bundle_token=token,
        timings=timings,
    )
    enrichment_json = canonical_json(
        {"judgment": "PASS", "result": make_result()}
    )
    enrichment_zlib = compressed_json(enrichment_json)
    enrichment = AnalysisEnrichmentReport(
        identity=identity,
        judgment="PASS",
        canonical_result_json=enrichment_json,
        result_payload_zlib=enrichment_zlib,
        chart_manifest=(
            (
                0,
                ("fft", "loudness", "octave", "spl", "stft", "stft_score"),
            ),
        ),
        timings=timings,
    )
    assert decision.bundle_token.identity is decision.identity
    assert enrichment.result_payload_zlib == enrichment_zlib
    assert enrichment.chart_manifest[0][0] == 0
    assert tuple(AnalysisPhase) == (
        AnalysisPhase.DECISION,
        AnalysisPhase.ENRICHMENT,
    )
    with pytest.raises(ValueError, match="AnalysisPhase"):
        AnalysisPhase("unknown")


def test_enrichment_report_rejects_malformed_compressed_payload():
    identity = make_identity()
    canonical = canonical_json({"judgment": "PASS", "result": make_result()})
    with pytest.raises(ValueError, match="compressed|zlib"):
        AnalysisEnrichmentReport(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical,
            result_payload_zlib=b"not-a-zlib-stream",
            timings=AnalysisStageTimings(()),
        )


def test_enrichment_report_does_not_parse_full_chart_json(monkeypatch):
    identity = make_identity()
    canonical = canonical_json({"judgment": "PASS", "result": make_result()})
    monkeypatch.setattr(
        staged_result_payload.json,
        "loads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("full enrichment JSON was parsed")
        ),
    )

    report = AnalysisEnrichmentReport(
        identity=identity,
        judgment="PASS",
        canonical_result_json=canonical,
        result_payload_zlib=compressed_json(canonical),
        timings=AnalysisStageTimings(()),
        chart_manifest=((0, ("spl",)),),
    )

    assert report.chart_manifest == ((0, ("spl",)),)


@pytest.mark.parametrize(
    "manifest",
    [
        ((0, ("spl", "spl")),),
        ((0, ("stft", "spl")),),
        ((1, ("spl",)), (0, ("spl",))),
        ((0, ("spl",)), (0, ("stft",))),
    ],
)
def test_enrichment_report_rejects_duplicate_or_unsorted_manifest(manifest):
    identity = make_identity()
    canonical = canonical_json({"judgment": "PASS", "result": make_result()})

    with pytest.raises(ValueError, match="sorted|unique"):
        AnalysisEnrichmentReport(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical,
            result_payload_zlib=compressed_json(canonical),
            timings=AnalysisStageTimings(()),
            chart_manifest=manifest,
        )


@pytest.mark.parametrize("hostile", [bytearray(b"zlib"), memoryview(b"zlib")])
def test_enrichment_report_rejects_non_exact_compressed_byte_types(hostile):
    identity = make_identity()
    canonical = canonical_json({"judgment": "PASS", "result": make_result()})
    with pytest.raises(TypeError, match="exact bytes"):
        AnalysisEnrichmentReport(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical,
            result_payload_zlib=hostile,
            timings=AnalysisStageTimings(()),
        )


def test_enrichment_report_rejects_compression_for_different_canonical_result():
    identity = make_identity()
    canonical = canonical_json({"judgment": "PASS", "result": make_result()})
    different_result = make_result()
    different_result["chart_payload"] = {"different": True}
    different = canonical_json(
        {"judgment": "PASS", "result": different_result}
    )
    with pytest.raises(ValueError, match="match"):
        AnalysisEnrichmentReport(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical,
            result_payload_zlib=compressed_json(different),
            timings=AnalysisStageTimings(()),
        )


@pytest.mark.parametrize("trailing_kind", ["bytes", "second_stream"])
def test_enrichment_report_rejects_trailing_compressed_data(trailing_kind: str):
    identity = make_identity()
    canonical = canonical_json({"judgment": "PASS", "result": make_result()})
    trailing = (
        b"trailing"
        if trailing_kind == "bytes"
        else zlib.compress(b"second-stream")
    )
    with pytest.raises(ValueError, match="trailing|stream"):
        AnalysisEnrichmentReport(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical,
            result_payload_zlib=compressed_json(canonical) + trailing,
            timings=AnalysisStageTimings(()),
        )


def test_enrichment_report_enforces_compressed_size_limit(monkeypatch):
    identity = make_identity()
    canonical = canonical_json({"judgment": "PASS", "result": make_result()})
    compressed = compressed_json(canonical)
    monkeypatch.setattr(
        staged_execution,
        "_MAX_COMPRESSED_RESULT_BYTES",
        len(compressed) - 1,
    )
    with pytest.raises(ValueError, match="compressed.*size"):
        AnalysisEnrichmentReport(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical,
            result_payload_zlib=compressed,
            timings=AnalysisStageTimings(()),
        )


def test_enrichment_report_enforces_uncompressed_size_limit(monkeypatch):
    identity = make_identity()
    canonical = canonical_json({"judgment": "PASS", "result": make_result()})
    monkeypatch.setattr(
        staged_execution,
        "_MAX_CANONICAL_RESULT_BYTES",
        len(canonical.encode("utf-8")) - 1,
    )
    with pytest.raises(ValueError, match="canonical.*size"):
        AnalysisEnrichmentReport(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical,
            result_payload_zlib=compressed_json(canonical),
            timings=AnalysisStageTimings(()),
        )


@pytest.mark.parametrize(
    "malformed_root",
    [
        {"judgment": "PASS"},
        {"judgment": "PASS", "result": make_result(), "unknown": True},
        {"judgment": "PASS", "result": {}},
    ],
)
def test_decision_report_rejects_malformed_canonical_roots(malformed_root):
    identity = make_identity()
    token = AnalysisBundleToken(0, "bundle-1", identity)
    timings = AnalysisStageTimings(())
    canonical = canonical_json(malformed_root)
    with pytest.raises(ValueError, match="root|result fields"):
        AnalysisDecisionEnvelope(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical,
            summary_payload_json=canonical_json(make_result()["summary_payload"]),
            bundle_token=token,
            timings=timings,
        )


def test_decision_envelope_rejects_non_empty_charts():
    identity = make_identity()
    token = AnalysisBundleToken(0, "bundle-1", identity)
    result = make_result()
    with pytest.raises(ValueError, match="chart_payload"):
        AnalysisDecisionEnvelope(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical_json(
                {"judgment": "PASS", "result": result}
            ),
            summary_payload_json=canonical_json(result["summary_payload"]),
            bundle_token=token,
            timings=AnalysisStageTimings(()),
        )


def test_decision_envelope_rejects_mismatched_embedded_summary():
    identity = make_identity()
    token = AnalysisBundleToken(0, "bundle-1", identity)
    canonical = build_lightweight_canonical_result(
        judgment="PASS",
        result=make_result(),
    )
    with pytest.raises(ValueError, match="summary_payload_json"):
        AnalysisDecisionEnvelope(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical,
            summary_payload_json=canonical_json({"overall_status": "fail"}),
            bundle_token=token,
            timings=AnalysisStageTimings(()),
        )


def test_decision_envelope_compares_embedded_summary_types_exactly():
    identity = make_identity()
    token = AnalysisBundleToken(0, "bundle-1", identity)
    result = make_result()
    result["summary_payload"]["decision_pass"] = True  # type: ignore[index]
    canonical = build_lightweight_canonical_result(
        judgment="PASS",
        result=result,
    )
    summary = copy.deepcopy(result["summary_payload"])
    summary["decision_pass"] = 1
    with pytest.raises(ValueError, match="summary_payload_json"):
        AnalysisDecisionEnvelope(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical,
            summary_payload_json=canonical_json(summary),
            bundle_token=token,
            timings=AnalysisStageTimings(()),
        )


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_decision_report_rejects_non_finite_json_constants(constant: str):
    identity = make_identity()
    token = AnalysisBundleToken(0, "bundle-1", identity)
    result = make_result()
    result["chart_payload"] = {}
    canonical = canonical_json({"judgment": "PASS", "result": result})
    hostile = canonical.replace('"ai_score":null', f'"ai_score":{constant}')
    with pytest.raises(ValueError, match="JSON|constant|finite"):
        AnalysisDecisionEnvelope(
            identity=identity,
            judgment="PASS",
            canonical_result_json=hostile,
            summary_payload_json=canonical_json(result["summary_payload"]),
            bundle_token=token,
            timings=AnalysisStageTimings(()),
        )


def test_decision_report_rejects_duplicate_json_object_keys():
    identity = make_identity()
    token = AnalysisBundleToken(0, "bundle-1", identity)
    decision_json = build_lightweight_canonical_result(
        judgment="PASS",
        result=make_result(),
    )
    duplicate_summary = (
        '{"overall_label":"ok","overall_label":"ok",'
        '"overall_status":"pass","rows":[]}'
    )
    with pytest.raises(ValueError, match="duplicate"):
        AnalysisDecisionEnvelope(
            identity=identity,
            judgment="PASS",
            canonical_result_json=decision_json,
            summary_payload_json=duplicate_summary,
            bundle_token=token,
            timings=AnalysisStageTimings(()),
        )


@pytest.mark.parametrize("parser_error", [RecursionError(), OverflowError()])
def test_decision_report_normalizes_json_parser_depth_failures(
    monkeypatch,
    parser_error,
):
    identity = make_identity()

    def fail_parse(*args, **kwargs):
        del args, kwargs
        raise parser_error

    monkeypatch.setattr(staged_result_payload.json, "loads", fail_parse)
    with pytest.raises(ValueError, match="strict JSON"):
        AnalysisDecisionEnvelope(
            identity=identity,
            judgment="PASS",
            canonical_result_json=build_lightweight_canonical_result(
                judgment="PASS",
                result=make_result(),
            ),
            summary_payload_json=canonical_json(make_result()["summary_payload"]),
            bundle_token=AnalysisBundleToken(0, "bundle-1", identity),
            timings=AnalysisStageTimings(()),
        )


@pytest.mark.parametrize("noncanonical_kind", ["whitespace", "key_order"])
def test_staged_reports_reject_noncanonical_result_json(noncanonical_kind: str):
    identity = make_identity()
    root = {"judgment": "PASS", "result": make_result()}
    if noncanonical_kind == "whitespace":
        supplied = json.dumps(root, ensure_ascii=False, sort_keys=True)
    else:
        supplied = (
            '{"result":'
            f'{canonical_json(make_result())},"judgment":"PASS"}}'
        )
    with pytest.raises(ValueError, match="canonical"):
        AnalysisEnrichmentReport(
            identity=identity,
            judgment="PASS",
            canonical_result_json=supplied,
            result_payload_zlib=compressed_json(supplied),
            timings=AnalysisStageTimings(()),
        )


def test_decision_envelope_rejects_noncanonical_summary_json():
    identity = make_identity()
    with pytest.raises(ValueError, match="canonical"):
        AnalysisDecisionEnvelope(
            identity=identity,
            judgment="PASS",
            canonical_result_json=build_lightweight_canonical_result(
                judgment="PASS",
                result=make_result(),
            ),
            summary_payload_json=json.dumps(
                make_result()["summary_payload"],
                ensure_ascii=False,
                sort_keys=True,
            ),
            bundle_token=AnalysisBundleToken(0, "bundle-1", identity),
            timings=AnalysisStageTimings(()),
        )


def test_decision_envelope_rejects_lone_surrogate_in_result_field():
    identity = make_identity()
    result = make_result()
    result["legacy_overall"] = "\ud800"
    result["chart_payload"] = {}
    with pytest.raises(ValueError, match="UTF-8"):
        AnalysisDecisionEnvelope(
            identity=identity,
            judgment="PASS",
            canonical_result_json=canonical_json(
                {"judgment": "PASS", "result": result}
            ),
            summary_payload_json=canonical_json(result["summary_payload"]),
            bundle_token=AnalysisBundleToken(0, "bundle-1", identity),
            timings=AnalysisStageTimings(()),
        )


def test_lightweight_builder_rejects_lone_surrogate_in_result_field():
    result = make_result()
    result["legacy_overall"] = "\ud800"
    with pytest.raises(ValueError, match="UTF-8"):
        build_lightweight_canonical_result(
            judgment="PASS",
            result=result,
        )


def test_decision_envelope_rejects_lone_surrogate_in_summary_field():
    identity = make_identity()
    summary = copy.deepcopy(make_result()["summary_payload"])
    summary["overall_label"] = "\ud800"
    with pytest.raises(ValueError, match="UTF-8"):
        AnalysisDecisionEnvelope(
            identity=identity,
            judgment="PASS",
            canonical_result_json=build_lightweight_canonical_result(
                judgment="PASS",
                result=make_result(),
            ),
            summary_payload_json=canonical_json(summary),
            bundle_token=AnalysisBundleToken(0, "bundle-1", identity),
            timings=AnalysisStageTimings(()),
        )


def test_enrichment_report_rejects_lone_surrogate_in_result_field():
    identity = make_identity()
    result = make_result()
    result["legacy_overall"] = "\ud800"
    hostile = canonical_json({"judgment": "PASS", "result": result})
    compressed = zlib.compress(hostile.encode("utf-8", errors="surrogatepass"))
    with pytest.raises(ValueError, match="UTF-8"):
        AnalysisEnrichmentReport(
            identity=identity,
            judgment="PASS",
            canonical_result_json=hostile,
            result_payload_zlib=compressed,
            timings=AnalysisStageTimings(()),
        )


@pytest.mark.parametrize(
    ("timing_change", "message"),
    [
        ({"run_id": "run-2"}, "run_id"),
        ({"analysis_attempt_id": "attempt-2"}, "analysis_attempt_id"),
    ],
)
@pytest.mark.parametrize("report_kind", ["decision", "enrichment"])
def test_staged_reports_reject_timing_identity_mismatch(
    timing_change,
    message,
    report_kind,
):
    identity = make_identity()
    timings = AnalysisStageTimings((make_timing_event(**timing_change),))
    if report_kind == "decision":
        with pytest.raises(ValueError, match=message):
            AnalysisDecisionEnvelope(
                identity=identity,
                judgment="PASS",
                canonical_result_json=build_lightweight_canonical_result(
                    judgment="PASS",
                    result=make_result(),
                ),
                summary_payload_json=canonical_json(
                    make_result()["summary_payload"]
                ),
                bundle_token=AnalysisBundleToken(0, "bundle-1", identity),
                timings=timings,
            )
    else:
        enrichment_json = canonical_json(
            {"judgment": "PASS", "result": make_result()}
        )
        with pytest.raises(ValueError, match=message):
            AnalysisEnrichmentReport(
                identity=identity,
                judgment="PASS",
                canonical_result_json=enrichment_json,
                result_payload_zlib=compressed_json(enrichment_json),
                timings=timings,
            )


class _HostileChartValue:
    def __deepcopy__(self, memo):
        del memo
        raise AssertionError("chart value must not be copied")


def test_lightweight_payload_does_not_copy_or_traverse_chart_values():
    result = make_result()
    result["chart_payload"] = {"hostile": _HostileChartValue()}
    canonical = build_lightweight_canonical_result(
        judgment="PASS",
        result=result,
    )
    assert json.loads(canonical)["result"]["chart_payload"] == {}


def test_enrichment_comparison_never_copies_or_compares_chart_values(monkeypatch):
    identity = make_identity()
    decision_json = build_lightweight_canonical_result(
        judgment="PASS",
        result=make_result(),
    )
    enriched = make_result()
    enriched["chart_payload"] = {"marker": "chart-only-marker"}
    comparisons: list[tuple[object, object]] = []
    exact_compare = staged_result_payload.json_values_equal_exact

    def forbidden_deepcopy(value, memo=None):
        del value, memo
        raise AssertionError("compatibility validation must not deepcopy")

    def recording_compare(left, right):
        assert "chart-only-marker" not in repr((left, right))
        comparisons.append((left, right))
        return exact_compare(left, right)

    monkeypatch.setattr(copy, "deepcopy", forbidden_deepcopy)
    monkeypatch.setattr(
        staged_result_payload,
        "json_values_equal_exact",
        recording_compare,
    )
    validate_enrichment_compatibility(
        decision_identity=identity,
        enrichment_identity=identity,
        decision_canonical_result_json=decision_json,
        enrichment_canonical_result_json=canonical_json(
            {"judgment": "PASS", "result": enriched}
        ),
    )
    assert len(comparisons) == len(REQUIRED_RESULT_FIELDS)
