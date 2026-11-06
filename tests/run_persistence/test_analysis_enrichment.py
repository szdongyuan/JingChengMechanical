from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
import hashlib
from pathlib import Path
import threading
from uuid import uuid4
import zlib

import pytest

import app.service.run_persistence.attempt_repository as attempt_repository_module
from app.service.analysis import staged_result_payload
from app.service.analysis.staged_result_payload import (
    build_lightweight_canonical_result,
)
from app.service.analysis.scheduler import (
    AnalysisCancelOrigin,
    AnalysisBundleIdentity,
    AnalysisEnrichmentReport,
    AnalysisErrorCategory,
    AnalysisStage,
    AnalysisStageTimings,
    CancelStatus,
    FrozenResultObject,
    ShutdownStatus,
)
from app.service.run_persistence import (
    AnalysisEnrichmentPersistenceError,
    BridgeCancelRequest,
    BridgeCancelStatus,
    EnrichmentCommittedEvent,
    ReanalysisRequest,
)
from app.service.run_persistence.analysis_result_artifact import (
    AnalysisResultArtifactStore,
)
from app.service.run_persistence import run_persistence_scheduler_bridge as bridge_module
from app.service.run_persistence.attempt_repository import (
    AttemptOutcome,
    AttemptRepository,
)
from app.service.run_persistence.audio_repository import AudioRepository
from app.service.run_persistence.contracts import (
    IngressKind,
    RunInsert,
    TriggerSource,
    freeze_run_inputs,
)
from app.service.run_persistence.run_repository import RunRepository
from app.service.run_persistence.export_reader import RunExportReader
from app.service.multi_run_integration.production_composition import (
    _CoordinatorPersistence,
)
from app.service.run_persistence.contracts import (
    AttemptState,
    Judgment,
    TransactionResult,
    TransactionState,
)
from tests.run_persistence.test_attempt_repository import (
    LATER,
    attempt_insert,
    seed_audio,
)
from tests.run_persistence.test_audio_repository import ready_runtime
from tests.run_persistence.test_audio_repository import fact
from tests.run_persistence.conftest import make_analysis_envelope
from tests.run_persistence.test_scheduler_bridge_lifecycle import (
    RUN_ID,
    _Scheduler,
    _cancel_receipt,
    _failed_outcome,
    _make_bridge,
    _reference,
    _run_record,
    _shutdown_report,
    _succeeded_outcome,
)


ENRICHED_AT = "2026-07-27T12:36:00.000000Z"
_REQUIRED_CHART_METRICS = (
    "fft",
    "loudness",
    "octave",
    "spl",
    "stft",
    "stft_score",
)


def _chart_manifest(*channels: int):
    selected = channels or (0,)
    return tuple((channel, _REQUIRED_CHART_METRICS) for channel in selected)


def _identity_for_job(job, **changes: object) -> AnalysisBundleIdentity:
    digest = lambda value: "sha256:" + hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()
    values = {
        "run_id": job.run_id,
        "analysis_attempt_id": job.analysis_attempt_id,
        "content_revision": job.audio.content_revision,
        "algorithm_revision": job.snapshot.algorithm_revision,
        "config_identity": digest(job.snapshot.config_snapshot_json),
        "calibration_identity": digest(
            job.snapshot.calibration_snapshot_json
        ),
        "channel_mapping_identity": digest(job.snapshot.channel_mapping_json),
        "window_start_seconds": float(
            job.snapshot.analysis_window_start_seconds
        ),
        "window_end_seconds": float(job.snapshot.analysis_window_end_seconds),
    }
    values.update(changes)
    return AnalysisBundleIdentity(**values)


def test_expected_chart_manifest_respects_exact_selected_vibration_role(
    tmp_path: Path,
) -> None:
    bridge, _boundary, scheduler, _attempts, _reports, audio = _make_bridge(
        tmp_path
    )
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    config = json.loads(job.snapshot.config_snapshot_json)

    assert bridge_module._expected_chart_manifest_for_job(job) == _chart_manifest()

    config["vibration_view_channel_index"] = 0
    vibration_job = replace(
        job,
        snapshot=replace(
            job.snapshot,
            config_snapshot_json=json.dumps(config),
        ),
    )
    assert bridge_module._expected_chart_manifest_for_job(vibration_job) == (
        (0, ("fft", "octave", "stft")),
    )

    for invalid in (True, "0", 99):
        config["vibration_view_channel_index"] = invalid
        malformed_job = replace(
            job,
            snapshot=replace(
                job.snapshot,
                config_snapshot_json=json.dumps(config),
            ),
        )
        assert bridge_module._expected_chart_manifest_for_job(malformed_job) is None


def _result(*, judgment: str = "PASS", charts: bool) -> dict[str, object]:
    passed = judgment == "PASS"
    return {
        "legacy_overall": "qualified" if passed else "unqualified",
        "limit_pass": passed,
        "ai_pass": None,
        "ai_score": None,
        "analysis_window": [0.0, 1.0],
        "channel_results": [
            {"channel_index": 0, "spl_pass": passed, "fft_available": True}
        ],
        "review": {"algorithm_revision": "algorithm-1"},
        "displayed_config_id": 1,
        "config_payload": {"selected_channels": [0]},
        "captured_duration_sec": 1.0,
        "default_window": {"start_sec": 0.0, "end_sec": 1.0},
        "fallback_window": {"start_sec": 0.0, "end_sec": 1.0},
        "manual_reanalysis_allowed": True,
        "judgment_payload": {
            "overall": judgment,
            "channel_results": [{"channel_index": 0, "spl_pass": passed}],
        },
        "summary_payload": {
            "overall_status": "pass" if passed else "fail",
            "rows": [{"channel_index": 0, "passed": passed}],
        },
        "chart_payload": (
            {"0": {"spl": {"times": [0.0], "values": [61.25]}}}
            if charts
            else {}
        ),
    }


def _canonical_full(*, judgment: str = "PASS") -> str:
    return json.dumps(
        {"judgment": judgment, "result": _result(judgment=judgment, charts=True)},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_lightweight(*, judgment: str = "PASS") -> str:
    return build_lightweight_canonical_result(
        judgment=judgment,
        result=_result(judgment=judgment, charts=False),
    )


def _successful_outcome(payload: str, judgment: Judgment = Judgment.PASS):
    return AttemptOutcome(
        state=AttemptState.SUCCEEDED,
        judgment=judgment,
        result_payload_json=payload,
        code=None,
        summary="decision committed",
        result_payload_zlib=zlib.compress(payload.encode("utf-8")),
    )


def _freeze(value: object):
    if type(value) is dict:
        return FrozenResultObject(
            items=tuple((key, _freeze(item)) for key, item in value.items())
        )
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


class _StagedDecisionLifecycle:
    def __init__(self) -> None:
        self.committed: list[str] = []
        self.retired: list[tuple[str, str]] = []

    def mark_decision_committed(self, attempt_id: str) -> bool:
        self.committed.append(attempt_id)
        return True

    def retire_staged_attempt(self, attempt_id: str, reason_code: str) -> bool:
        self.retired.append((attempt_id, reason_code))
        return True


def test_boundary_releases_enrichment_only_after_durable_success_and_retires_failure(
    tmp_path: Path,
) -> None:
    committed = _StagedDecisionLifecycle()
    bridge, boundary, scheduler, _attempts, _reports, audio = _make_bridge(
        tmp_path,
    )
    boundary.attach_staged_executor(committed)
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)

    lightweight = _canonical_lightweight()
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(lightweight)),
        )
    )

    assert committed.committed == [job.analysis_attempt_id]
    assert committed.retired == []

    failed = _StagedDecisionLifecycle()
    bridge, boundary, scheduler, _attempts, _reports, audio = _make_bridge(
        tmp_path,
    )
    boundary.attach_staged_executor(failed)
    bridge.submit_initial(_make_initial_request(audio))
    failed_job = scheduler.submitted[0]
    boundary.before_audio_load(failed_job)
    boundary(
        _failed_outcome(
            failed_job,
            AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
            AnalysisStage.EXECUTE,
        )
    )

    assert failed.committed == []
    assert failed.retired == [
        (failed_job.analysis_attempt_id, "ANALYSIS_EXECUTION_FAILED")
    ]


def test_lightweight_decision_is_canonical_terminal_and_enrichment_is_nonterminal(
    tmp_path: Path,
) -> None:
    enrichment_events: list[EnrichmentCommittedEvent] = []
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        enrichment_sink=enrichment_events.append,
    )
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    lightweight = _canonical_lightweight()
    decision_outcome = _succeeded_outcome(
        job,
        payload=_freeze(json.loads(lightweight)),
    )

    first = boundary(decision_outcome)
    duplicate = boundary(decision_outcome)
    conflicting = _succeeded_outcome(
        job,
        payload=_freeze(
            json.loads(_canonical_lightweight(judgment="FAIL"))
        ),
    )

    assert first is reports[0]
    assert duplicate is first
    assert boundary(conflicting) is None
    assert len(attempts.running_finished) == 1
    persisted = attempts.running_finished[0][1]
    assert persisted.result_payload_json == lightweight
    assert json.loads(lightweight)["result"]["chart_payload"] == {}

    identity = _identity_for_job(job)
    full = _canonical_full()
    enrichment = AnalysisEnrichmentReport(
        identity=identity,
        judgment="PASS",
        canonical_result_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=_chart_manifest(),
    )

    event = boundary.accept_enrichment(enrichment)

    assert type(event) is EnrichmentCommittedEvent
    assert event is enrichment_events[0]
    assert event.identity is identity
    assert event.result_payload_json == full
    assert event.judgment is Judgment.PASS
    assert len(attempts.replacements) == 1
    assert len(attempts.running_finished) == 1
    assert boundary.accept_enrichment(enrichment) is None
    assert len(enrichment_events) == 1
    assert boundary(decision_outcome) is first
    assert len(attempts.running_finished) == 1
    assert boundary._pending_enrichment_count == 0
    assert boundary._terminal_replay_count == 1
    with pytest.raises(FrozenInstanceError):
        event.result_payload_json = lightweight  # type: ignore[misc]


@pytest.mark.parametrize("chart_payload", [{}, {"loading": {}}])
def test_incomplete_enrichment_does_not_claim_or_settle_before_full_charts(
    tmp_path: Path,
    chart_payload: dict[str, object],
) -> None:
    events: list[EnrichmentCommittedEvent] = []
    bridge, boundary, scheduler, attempts, _reports, audio = _make_bridge(
        tmp_path,
        enrichment_sink=events.append,
    )
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    lightweight = _canonical_lightweight()
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(lightweight)),
        )
    )
    identity = _identity_for_job(job)
    incomplete_result = _result(charts=False)
    incomplete_result["chart_payload"] = chart_payload
    incomplete_json = json.dumps(
        {"judgment": "PASS", "result": incomplete_result},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    incomplete = AnalysisEnrichmentReport(
        identity=identity,
        judgment="PASS",
        canonical_result_json=incomplete_json,
        result_payload_zlib=zlib.compress(incomplete_json.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=(),
    )

    with pytest.raises(AnalysisEnrichmentPersistenceError):
        boundary.accept_enrichment(incomplete)

    assert attempts.replacements == []
    assert events == []
    full = _canonical_full()
    complete = AnalysisEnrichmentReport(
        identity=identity,
        judgment="PASS",
        canonical_result_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=_chart_manifest(),
    )
    assert boundary.accept_enrichment(complete) is events[0]
    assert len(attempts.replacements) == 1


def test_committed_noop_retires_active_enrichment_without_emitting(
    tmp_path: Path,
) -> None:
    events: list[EnrichmentCommittedEvent] = []
    bridge, boundary, scheduler, attempts, _reports, audio = _make_bridge(
        tmp_path,
        enrichment_sink=events.append,
    )
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(_canonical_lightweight())),
        )
    )
    identity = _identity_for_job(job)
    full = _canonical_full()
    enrichment = AnalysisEnrichmentReport(
        identity=identity,
        judgment="PASS",
        canonical_result_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=_chart_manifest(),
    )
    attempts.terminal_result = TransactionResult(
        state=TransactionState.COMMITTED,
        value=False,
    )

    assert boundary.accept_enrichment(enrichment) is None
    assert events == []
    assert len(attempts.replacements) == 1
    assert boundary._pending_enrichment_count == 0
    assert boundary._terminal_replay_count == 1

    attempts.terminal_result = TransactionResult(
        state=TransactionState.COMMITTED,
        value=True,
    )
    assert boundary.accept_enrichment(enrichment) is None
    assert len(attempts.replacements) == 1
    assert events == []


def test_enrichment_ignores_stale_identity_and_retries_only_database_failure(
    tmp_path: Path,
) -> None:
    events: list[EnrichmentCommittedEvent] = []
    bridge, boundary, scheduler, attempts, _reports, audio = _make_bridge(
        tmp_path,
        enrichment_sink=events.append,
    )
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    lightweight = _canonical_lightweight()
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(lightweight)),
        )
    )
    base_identity = _identity_for_job(job)
    full = _canonical_full()

    def enrichment(identity: AnalysisBundleIdentity) -> AnalysisEnrichmentReport:
        return AnalysisEnrichmentReport(
            identity=identity,
            judgment="PASS",
            canonical_result_json=full,
            result_payload_zlib=zlib.compress(full.encode("utf-8")),
            timings=AnalysisStageTimings(()),
            chart_manifest=_chart_manifest(),
        )

    stale = replace(base_identity, content_revision="stale-content")
    assert boundary.accept_enrichment(enrichment(stale)) is None
    assert attempts.replacements == []
    assert boundary._pending_enrichment_count == 1

    attempts.terminal_result = TransactionResult(
        state=TransactionState.ROLLED_BACK,
        reason="database busy",
    )
    with pytest.raises(AnalysisEnrichmentPersistenceError, match="database busy"):
        boundary.accept_enrichment(enrichment(base_identity))
    assert events == []
    assert boundary._pending_enrichment_count == 1

    attempts.terminal_result = TransactionResult(
        state=TransactionState.COMMITTED,
        value=True,
    )
    assert boundary.accept_enrichment(enrichment(base_identity)) is events[0]
    assert len(attempts.replacements) == 2
    assert boundary._pending_enrichment_count == 0


def test_second_known_enrichment_failure_retires_active_state(
    tmp_path: Path,
) -> None:
    bridge, boundary, scheduler, attempts, _reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(_canonical_lightweight())),
        )
    )
    full = _canonical_full()
    enrichment = AnalysisEnrichmentReport(
        identity=_identity_for_job(job),
        judgment="PASS",
        canonical_result_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=_chart_manifest(),
    )
    attempts.terminal_result = TransactionResult(
        state=TransactionState.ROLLED_BACK,
        reason="database busy",
    )

    with pytest.raises(AnalysisEnrichmentPersistenceError, match="database busy"):
        boundary.accept_enrichment(enrichment)
    assert boundary._pending_enrichment_count == 1

    with pytest.raises(AnalysisEnrichmentPersistenceError, match="database busy"):
        boundary.accept_enrichment(enrichment)
    assert boundary._pending_enrichment_count == 0
    assert boundary._terminal_replay_count == 1
    assert boundary.accept_enrichment(enrichment) is None
    assert len(attempts.replacements) == 2


def test_unknown_enrichment_transaction_retires_active_state_immediately(
    tmp_path: Path,
) -> None:
    bridge, boundary, scheduler, attempts, _reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(_canonical_lightweight())),
        )
    )
    full = _canonical_full()
    enrichment = AnalysisEnrichmentReport(
        identity=_identity_for_job(job),
        judgment="PASS",
        canonical_result_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=_chart_manifest(),
    )
    attempts.terminal_result = TransactionResult(
        state=TransactionState.UNKNOWN,
        reason="commit state unknown",
        restart_required=True,
    )

    with pytest.raises(AnalysisEnrichmentPersistenceError, match="unknown"):
        boundary.accept_enrichment(enrichment)

    assert boundary._pending_enrichment_count == 0
    assert boundary._terminal_replay_count == 1
    assert boundary.accept_enrichment(enrichment) is None
    assert len(attempts.replacements) == 1


@pytest.mark.parametrize(
    ("field_name", "replacement_value"),
    [
        ("algorithm_revision", "different-algorithm"),
        ("config_identity", "sha256:" + "1" * 64),
        ("calibration_identity", "sha256:" + "2" * 64),
        ("channel_mapping_identity", "sha256:" + "3" * 64),
        ("window_start_seconds", 0.25),
        ("window_end_seconds", 2.0),
    ],
)
def test_enrichment_requires_the_full_committed_bundle_identity(
    tmp_path: Path,
    field_name: str,
    replacement_value: object,
) -> None:
    bridge, boundary, scheduler, attempts, _reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(_canonical_lightweight())),
        )
    )
    stale_identity = replace(
        _identity_for_job(job),
        **{field_name: replacement_value},
    )
    full = _canonical_full()
    stale = AnalysisEnrichmentReport(
        identity=stale_identity,
        judgment="PASS",
        canonical_result_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=_chart_manifest(),
    )

    assert boundary.accept_enrichment(stale) is None
    assert attempts.replacements == []


@pytest.mark.parametrize(
    "manifest",
    [
        (),
        ((1, _REQUIRED_CHART_METRICS),),
        ((0, _REQUIRED_CHART_METRICS[:-1]),),
    ],
)
def test_enrichment_manifest_must_match_selected_channels_and_metrics(
    tmp_path: Path,
    manifest: tuple[tuple[int, tuple[str, ...]], ...],
) -> None:
    bridge, boundary, scheduler, attempts, _reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(_canonical_lightweight())),
        )
    )
    full = _canonical_full()
    enrichment = AnalysisEnrichmentReport(
        identity=_identity_for_job(job),
        judgment="PASS",
        canonical_result_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=manifest,
    )

    with pytest.raises(
        AnalysisEnrichmentPersistenceError,
        match="committed Decision",
    ):
        boundary.accept_enrichment(enrichment)
    assert attempts.replacements == []
    assert boundary._pending_enrichment_count == 1
    assert boundary.retire_enrichment(
        enrichment.identity,
        "ENRICHMENT_MANIFEST_INVALID",
    ) is True
    assert boundary.retire_enrichment(
        enrichment.identity,
        "ENRICHMENT_MANIFEST_INVALID",
    ) is False
    assert boundary._pending_enrichment_count == 0


def test_retirement_ignores_foreign_full_identity(tmp_path: Path) -> None:
    bridge, boundary, scheduler, _attempts, _reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(_canonical_lightweight())),
        )
    )
    identity = _identity_for_job(job)
    foreign = replace(identity, algorithm_revision="foreign")

    assert boundary.retire_enrichment(
        foreign,
        "ENRICHMENT_COMPUTE_FAILED",
    ) is False
    assert boundary._pending_enrichment_count == 1
    assert boundary.retire_enrichment(
        identity,
        "ENRICHMENT_COMPUTE_FAILED",
    ) is True
    assert boundary._pending_enrichment_count == 0


def test_persistence_accepts_large_enrichment_without_parsing_or_rebuilding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[EnrichmentCommittedEvent] = []
    bridge, boundary, scheduler, attempts, _reports, audio = _make_bridge(
        tmp_path,
        enrichment_sink=events.append,
    )
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(_canonical_lightweight())),
        )
    )
    result = _result(charts=True)
    result["chart_payload"]["0"]["stft"] = {
        "magnitude_db": list(range(100_000))
    }
    full = json.dumps(
        {"judgment": "PASS", "result": result},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    enrichment = AnalysisEnrichmentReport(
        identity=_identity_for_job(job),
        judgment="PASS",
        canonical_result_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=_chart_manifest(),
    )
    monkeypatch.setattr(
        bridge_module,
        "decode_and_validate_canonical_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("full enrichment JSON was parsed")
        ),
    )
    monkeypatch.setattr(
        staged_result_payload,
        "build_lightweight_canonical_result",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("lightweight payload was rebuilt")
        ),
    )

    assert boundary.accept_enrichment(enrichment) is events[0]
    assert len(attempts.replacements) == 1


def test_reanalysis_admission_retires_late_enrichment_from_prior_attempt(
    tmp_path: Path,
) -> None:
    bridge, boundary, scheduler, attempts, _reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(_make_initial_request(audio))
    initial_job = scheduler.submitted[0]
    boundary.before_audio_load(initial_job)
    boundary(
        _succeeded_outcome(
            initial_job,
            payload=_freeze(json.loads(_canonical_lightweight())),
        )
    )
    identity = _identity_for_job(initial_job)
    full = _canonical_full()
    late = AnalysisEnrichmentReport(
        identity=identity,
        judgment="PASS",
        canonical_result_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=_chart_manifest(),
    )
    bridge.run_repository.record = _run_record(state="SUCCEEDED")

    submitted = bridge.submit_reanalysis(
        ReanalysisRequest(RUN_ID, 0.1, 0.9)
    )

    assert submitted.scheduler_accepted is True
    assert boundary.accept_enrichment(late) is None
    assert attempts.replacements == []
    assert boundary._pending_enrichment_count == 0


def test_closed_persistence_ignores_late_enrichment(tmp_path: Path) -> None:
    bridge, boundary, scheduler, attempts, _reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(_canonical_lightweight())),
        )
    )
    identity = _identity_for_job(job)
    full = _canonical_full()
    late = AnalysisEnrichmentReport(
        identity=identity,
        judgment="PASS",
        canonical_result_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=_chart_manifest(),
    )
    bridge.runtime._closed = True

    assert boundary.accept_enrichment(late) is None
    assert attempts.replacements == []


def test_terminal_replay_is_bounded_while_completed_enrichment_state_is_released(
    tmp_path: Path,
) -> None:
    bridge, boundary, scheduler, _attempts, reports, audio = _make_bridge(tmp_path)
    outcomes = []
    full = _canonical_full()

    for _index in range(65):
        bridge.submit_initial(_make_initial_request(audio))
        job = scheduler.submitted[-1]
        boundary.before_audio_load(job)
        outcome = _succeeded_outcome(
            job,
            payload=_freeze(json.loads(_canonical_lightweight())),
        )
        outcomes.append(outcome)
        boundary(outcome)
        enrichment = AnalysisEnrichmentReport(
            identity=_identity_for_job(job),
            judgment="PASS",
            canonical_result_json=full,
            result_payload_zlib=zlib.compress(full.encode("utf-8")),
            timings=AnalysisStageTimings(()),
            chart_manifest=_chart_manifest(),
        )
        assert boundary.accept_enrichment(enrichment) is not None

    assert boundary._pending_enrichment_count == 0
    assert boundary._terminal_replay_count == 64
    assert boundary(outcomes[0]) is None
    assert boundary(outcomes[-1]) is reports[-1]


def test_scheduler_shutdown_releases_pending_enrichment_state(
    tmp_path: Path,
) -> None:
    scheduler = _Scheduler(
        shutdown_value=_shutdown_report(ShutdownStatus.CLOSED)
    )
    bridge, boundary, scheduler, _attempts, _reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    boundary(
        _succeeded_outcome(
            job,
            payload=_freeze(json.loads(_canonical_lightweight())),
        )
    )
    assert boundary._pending_enrichment_count == 1

    bridge.stop_and_join_scheduler(1.0)

    assert boundary._pending_enrichment_count == 0


def test_cancel_after_decision_releases_pending_enrichment_but_keeps_replay(
    tmp_path: Path,
) -> None:
    scheduler = _Scheduler()
    bridge, boundary, scheduler, _attempts, _reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    bridge.submit_initial(_make_initial_request(audio))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    outcome = _succeeded_outcome(
        job,
        payload=_freeze(json.loads(_canonical_lightweight())),
    )
    terminal = boundary(outcome)
    scheduler.cancel_value = _cancel_receipt(
        job.run_id,
        job.analysis_attempt_id,
        CancelStatus.ALREADY_TERMINAL,
    )

    canceled = bridge.cancel(
        BridgeCancelRequest(
            run_id=job.run_id,
            analysis_attempt_id=job.analysis_attempt_id,
            origin=AnalysisCancelOrigin.USER_STOP,
            reason="retire pending enrichment",
        )
    )

    assert canceled.status is BridgeCancelStatus.ALREADY_TERMINAL
    assert boundary._pending_enrichment_count == 0
    assert boundary(outcome) is terminal


def _make_initial_request(audio):
    from app.service.run_persistence import InitialAnalysisRequest

    return InitialAnalysisRequest(RUN_ID, _reference(audio))


def _seed_succeeded_decision(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
    *,
    fault_types: tuple[str, ...] = (),
):
    runtime = ready_runtime(prepared_database_path)
    if not fault_types:
        run_id, audio = seed_audio(runtime, tmp_path, now)
    else:
        run_id = str(uuid4())
        assert RunRepository(runtime).insert_active(
            RunInsert(
                run_id=run_id,
                ingress_kind=IngressKind.CAPTURED,
                trigger_source=TriggerSource.MANUAL,
                config_id=1,
                hardware_id=2,
                snapshots=freeze_run_inputs(
                    make_analysis_envelope(
                        config={
                            "config_id": 1,
                            "config_name": "Config 1",
                            "fault_types": list(fault_types),
                            "selected_channels": [0],
                        }
                    )
                ),
                accepted_at=now,
            )
        ).value
        final = tmp_path / f"{run_id}.wav"
        final.write_bytes(b"completed")
        audio = fact(run_id, final, now)
        assert AudioRepository(runtime).insert_verified(audio).value
    store = AnalysisResultArtifactStore(tmp_path / "results")
    repository = AttemptRepository(runtime, result_artifact_store=store)
    attempt = attempt_insert(run_id, audio.audio_id, now)
    lightweight = _canonical_lightweight()
    assert repository.create_queued(attempt).value is True
    assert repository.mark_running(attempt.attempt_id, LATER).value is True
    assert repository.finish_running_and_project(
        attempt.attempt_id,
        _successful_outcome(lightweight),
        "2026-07-27T12:35:30.000000Z",
    ).value is True
    with runtime.operation() as database:
        row = database.execute_sql(
            """
            SELECT result_payload_json, terminal_at
            FROM analysis_attempt
            WHERE attempt_id = ?
            """,
            (attempt.attempt_id,),
        ).fetchone()
    return runtime, repository, store, run_id, attempt, lightweight, row


def test_replace_succeeded_result_publishes_then_guards_and_retires_lightweight(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
) -> None:
    (
        runtime,
        repository,
        store,
        run_id,
        attempt,
        lightweight,
        (old_reference, terminal_at),
    ) = _seed_succeeded_decision(prepared_database_path, tmp_path, now)
    old_path = store.path_for_reference(old_reference)
    full = _canonical_full()

    result = repository.replace_succeeded_result(
        attempt_id=attempt.attempt_id,
        run_id=run_id,
        judgment=Judgment.PASS,
        expected_payload_revision=store.payload_revision(lightweight),
        result_payload_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        now=ENRICHED_AT,
    )

    with runtime.operation() as database:
        stored_reference, stored_terminal_at = database.execute_sql(
            """
            SELECT result_payload_json, terminal_at
            FROM analysis_attempt
            WHERE attempt_id = ?
            """,
            (attempt.attempt_id,),
        ).fetchone()
    assert result == TransactionResult(
        state=TransactionState.COMMITTED,
        value=True,
    )
    assert stored_reference != old_reference
    assert store.load_reference(stored_reference, expected_judgment="PASS") == full
    assert stored_terminal_at == terminal_at
    assert old_path.exists() is False
    assert repository.latest_successful_for_run(run_id).result_payload_json == full


def test_enrichment_artifact_replacement_preserves_manual_result_control_facts(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
) -> None:
    (
        runtime,
        repository,
        store,
        run_id,
        attempt,
        lightweight,
        (_old_reference, terminal_at),
    ) = _seed_succeeded_decision(
        prepared_database_path,
        tmp_path,
        now,
        fault_types=("异响",),
    )
    runs = RunRepository(runtime)
    for expected_revision in range(4):
        assert runs.advance_causal_revision(
            run_id,
            expected_revision=expected_revision,
            proposed_revision=expected_revision + 1,
        ).value
    assert runs.set_manual_judgment(
        run_id,
        Judgment.FAIL,
        ENRICHED_AT,
        expected_revision=4,
        fault_type="异响",
        accepted_fault_types=("异响",),
    ).value
    assert runs.advance_causal_revision(
        run_id,
        expected_revision=4,
        proposed_revision=5,
    ).value
    before = runs.get(run_id)
    full = _canonical_full()

    replaced = repository.replace_succeeded_result(
        attempt_id=attempt.attempt_id,
        run_id=run_id,
        judgment=Judgment.PASS,
        expected_payload_revision=store.payload_revision(lightweight),
        result_payload_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        now=ENRICHED_AT,
    )

    after = runs.get(run_id)
    facts = _CoordinatorPersistence(
        runtime,
        result_artifact_store=store,
    ).load_result_control_facts(run_id)
    export_reader = RunExportReader(
        runtime,
        result_artifact_store=store,
    )
    assert replaced == TransactionResult(
        state=TransactionState.COMMITTED,
        value=True,
    )
    assert facts is not None, (
        after.state,
        after.judgment,
        after.causal_revision,
        repository.latest_successful_for_run(run_id),
    )
    detail_payload = json.loads(
        facts.current_projection.result_payload.canonical_bytes
    )
    assert detail_payload["result"]["chart_payload"] == json.loads(full)[
        "result"
    ]["chart_payload"]
    assert facts.current_projection.conclusion.name == "NG"
    assert facts.current_projection.fault_type == "异响"
    assert facts.current_projection.manual_judgment_revision == 5
    assert after.judgment == before.judgment == "FAIL"
    assert after.judgment_source == before.judgment_source == "MANUAL"
    assert after.fault_type == before.fault_type == "异响"
    assert after.manual_judgment_revision == before.manual_judgment_revision == 5
    assert after.causal_revision == before.causal_revision == 5
    assert export_reader.pending_enrichment_run_ids((run_id,)) == ()
    (export_record,) = export_reader.read_many((run_id,))
    assert export_record.judgment == "FAIL"
    assert export_record.judgment_source == "MANUAL"
    assert export_record.fault_type == "异响"
    assert export_record.manual_judgment_revision == 5
    stored_attempts = repository.list_for_run(run_id)
    assert len(stored_attempts) == 1
    assert stored_attempts[0].terminal_at == terminal_at


def test_replace_rollback_removes_new_artifact_and_keeps_old_reference(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        runtime,
        repository,
        store,
        run_id,
        attempt,
        lightweight,
        (old_reference, _terminal_at),
    ) = _seed_succeeded_decision(prepared_database_path, tmp_path, now)
    old_path = store.path_for_reference(old_reference)
    full = _canonical_full()
    monkeypatch.setattr(
        attempt_repository_module,
        "run_short_transaction",
        lambda _database, _work: TransactionResult(
            state=TransactionState.ROLLED_BACK,
            reason="forced rollback",
        ),
    )

    result = repository.replace_succeeded_result(
        attempt_id=attempt.attempt_id,
        run_id=run_id,
        judgment=Judgment.PASS,
        expected_payload_revision=store.payload_revision(lightweight),
        result_payload_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        now=ENRICHED_AT,
    )

    with runtime.operation() as database:
        stored_reference = database.execute_sql(
            "SELECT result_payload_json FROM analysis_attempt WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()[0]
    assert result.state is TransactionState.ROLLED_BACK
    assert stored_reference == old_reference
    assert old_path.is_file()
    assert tuple(store.root.glob("*.json.zlib")) == (old_path,)


def test_duplicate_same_digest_is_idempotent_and_conflicts_are_ignored(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
) -> None:
    (
        runtime,
        repository,
        store,
        run_id,
        attempt,
        lightweight,
        _row,
    ) = _seed_succeeded_decision(prepared_database_path, tmp_path, now)
    full = _canonical_full()
    common = {
        "attempt_id": attempt.attempt_id,
        "run_id": run_id,
        "judgment": Judgment.PASS,
        "expected_payload_revision": store.payload_revision(lightweight),
        "result_payload_json": full,
        "result_payload_zlib": zlib.compress(full.encode("utf-8")),
        "now": ENRICHED_AT,
    }
    assert repository.replace_succeeded_result(**common).value is True

    duplicate = repository.replace_succeeded_result(**common)
    wrong_run = repository.replace_succeeded_result(
        **{**common, "run_id": "11111111-1111-4111-8111-111111111111"}
    )
    wrong_revision = repository.replace_succeeded_result(
        **{**common, "expected_payload_revision": "0" * 64}
    )
    conflicting_full = _canonical_full()
    conflicting_full = conflicting_full.replace("61.25", "62.5")
    conflicting = repository.replace_succeeded_result(
        **{
            **common,
            "result_payload_json": conflicting_full,
            "result_payload_zlib": zlib.compress(conflicting_full.encode("utf-8")),
        }
    )

    assert duplicate == TransactionResult(
        state=TransactionState.COMMITTED,
        value=False,
    )
    assert wrong_run.value is False
    assert wrong_revision.value is False
    assert conflicting.value is False
    assert repository.latest_successful_for_run(run_id).result_payload_json == full
    assert len(tuple(store.root.glob("*.json.zlib"))) == 1


def test_reader_observes_valid_old_payload_before_publish_and_new_after_commit(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _runtime,
        repository,
        store,
        run_id,
        attempt,
        lightweight,
        _row,
    ) = _seed_succeeded_decision(prepared_database_path, tmp_path, now)
    full = _canonical_full()
    original_write = repository._write
    observed_during_publication: list[str] = []

    def observe_old_then_write(work):
        record = repository.latest_successful_for_run(run_id)
        assert record is not None
        observed_during_publication.append(record.result_payload_json)
        return original_write(work)

    monkeypatch.setattr(repository, "_write", observe_old_then_write)
    result = repository.replace_succeeded_result(
        attempt_id=attempt.attempt_id,
        run_id=run_id,
        judgment=Judgment.PASS,
        expected_payload_revision=store.payload_revision(lightweight),
        result_payload_json=full,
        result_payload_zlib=zlib.compress(full.encode("utf-8")),
        now=ENRICHED_AT,
    )

    assert result.value is True
    assert observed_during_publication == [lightweight]
    assert repository.latest_successful_for_run(run_id).result_payload_json == full


def test_inflight_reader_finishes_old_payload_before_old_artifact_retirement(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        runtime,
        repository,
        store,
        run_id,
        attempt,
        lightweight,
        (old_reference, _terminal_at),
    ) = _seed_succeeded_decision(prepared_database_path, tmp_path, now)
    full = _canonical_full()
    unrelated_run_id, unrelated_audio = seed_audio(runtime, tmp_path, now)
    unrelated_attempt = attempt_insert(
        unrelated_run_id,
        unrelated_audio.audio_id,
        now,
    )
    assert repository.create_queued(unrelated_attempt).value is True
    original_load = store.load_read_lease
    reader_started = threading.Event()
    release_reader = threading.Event()
    replacement_finished = threading.Event()
    unrelated_finished = threading.Event()
    observed: list[str] = []
    errors: list[BaseException] = []

    def blocked_load(lease):
        reader_started.set()
        assert release_reader.wait(5)
        return original_load(lease)

    def read_old() -> None:
        try:
            record = repository.latest_successful_for_run(run_id)
            assert record is not None
            observed.append(record.result_payload_json)
        except BaseException as exc:
            errors.append(exc)

    def replace_payload() -> None:
        try:
            repository.replace_succeeded_result(
                attempt_id=attempt.attempt_id,
                run_id=run_id,
                judgment=Judgment.PASS,
                expected_payload_revision=store.payload_revision(lightweight),
                result_payload_json=full,
                result_payload_zlib=zlib.compress(full.encode("utf-8")),
                now=ENRICHED_AT,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            replacement_finished.set()

    def commit_unrelated_write() -> None:
        try:
            assert repository.mark_running(
                unrelated_attempt.attempt_id,
                ENRICHED_AT,
            ).value is True
        except BaseException as exc:
            errors.append(exc)
        finally:
            unrelated_finished.set()

    monkeypatch.setattr(store, "load_read_lease", blocked_load)
    reader = threading.Thread(target=read_old)
    replacement = threading.Thread(target=replace_payload)
    unrelated = threading.Thread(target=commit_unrelated_write)
    reader.start()
    assert reader_started.wait(5)
    unrelated.start()
    replacement.start()
    assert unrelated_finished.wait(1)
    assert replacement_finished.wait(1)
    assert store.path_for_reference(old_reference).is_file()
    release_reader.set()
    reader.join(5)
    replacement.join(5)
    unrelated.join(5)

    assert reader.is_alive() is False
    assert replacement.is_alive() is False
    assert unrelated.is_alive() is False
    assert errors == []
    assert observed == [lightweight]
    assert store.path_for_reference(old_reference).exists() is False
    monkeypatch.setattr(store, "load_read_lease", original_load)
    assert repository.latest_successful_for_run(run_id).result_payload_json == full
