from __future__ import annotations

from dataclasses import replace
import struct

import pytest

from app.service.current_meter_service import CurrentSample
from app.service.multi_run_coordinator_mvp import (
    AudioMetadataConstraints,
    AudioSaveOutcome,
    AudioSaveStatus,
    CapturedAudioValue,
    CapturedSourceFormat,
    CaptureOutcome,
    CaptureOutcomeStatus,
    CommitResult,
    HistoryFactKind,
    OperationStatus,
    PersistedAudioFormat,
    StartStatus,
    TerminalErrorCategory,
    TriggerSource,
)
from app.service.multi_run_integration.shutdown import ApplicationShutdownOwner
from app.service.run_persistence import AttemptState, BridgeTerminalStatus, Judgment
from app.service.run_scoped_playback.contracts import PlaybackCode
from tests.multi_run_coordinator_mvp._support import (
    ATTEMPT_IDS,
    AUDIO_IDS,
    RUN_IDS,
    FakeCurrentCollector,
    Harness,
    captured_request,
    completed_capture,
    frozen_captured,
    saved_outcome,
)
from tests.multi_run_coordinator_mvp.test_bridge_initial import (
    drive_to_bridge,
    terminal_report,
)
from tests.multi_run_coordinator_mvp.test_result_operations import (
    manual_request,
    result_facts,
)
from tests.multi_run_integration.test_shutdown_order import (
    _Clock,
    _harness as shutdown_harness,
    _non_clean_result,
)
from tests.run_scoped_playback.conftest import (
    OTHER_RUN_ID,
    RUN_ID as PLAYBACK_RUN_ID,
    FakeAudioLoader,
    FakeAudioRepository,
    FakeBackend,
    FakeRunRepository,
    SequenceClock,
    make_run,
)
from tests.run_scoped_playback.test_service_resolution import (
    make_service as make_playback_service,
    request as playback_request,
)


def _failed_capture(run_id, reservation, reason="capture failed"):
    return CaptureOutcome(
        run_id=run_id,
        reservation=reservation,
        status=CaptureOutcomeStatus.FAILED,
        audio=None,
        error_category=TerminalErrorCategory.CAPTURE_FAILED,
        reason=reason,
    )


def _start_two_at_bridge() -> tuple[Harness, object]:
    harness = Harness()
    harness.scheduler.bridge_results.extend(
        (
            harness.scheduler.accepted_bridge_result(
                RUN_IDS[0],
                ATTEMPT_IDS[0],
            ),
            harness.scheduler.accepted_bridge_result(
                RUN_IDS[1],
                ATTEMPT_IDS[1],
            ),
        )
    )
    coordinator = harness.coordinator()
    for run_id in RUN_IDS[:2]:
        assert coordinator.start_captured(captured_request(run_id)).accepted
        reservation = next(
            item for item in harness.resident.register_calls if item.run_id == run_id
        )
        harness.resident.complete(completed_capture(reservation))
        save_request, save_sink = next(
            pair
            for pair in harness.save_dispatcher.calls
            if pair[0].run_id == run_id
        )
        outcome = saved_outcome(save_request)
        outcome = replace(
            outcome,
            persisted_audio=replace(
                outcome.persisted_audio,
                audio_id=AUDIO_IDS[RUN_IDS.index(run_id)],
            ),
        )
        save_sink(outcome)
    return harness, coordinator


def test_cross_aggregate_one_attempt_failure_matrix_is_isolated():
    # Preaccept failure owns no durable row or Resident resource; a sibling can start.
    preaccept = Harness()
    preaccept.captured_freezer.exception = RuntimeError("invalid frozen input")
    preaccept_coordinator = preaccept.coordinator()
    rejected = preaccept_coordinator.start_captured(
        captured_request(RUN_IDS[0], source=TriggerSource.PLC)
    )
    assert rejected.status is StartStatus.PREFLIGHT_REJECTED
    assert preaccept.persistence.active_calls == []
    assert preaccept.resident.reserve_calls == []
    assert len(preaccept.plc.reject_calls) == 1
    preaccept.captured_freezer.exception = None
    assert preaccept_coordinator.start_captured(
        captured_request(RUN_IDS[1])
    ).accepted

    # A capture terminal is consumed once and cannot take its sibling's reservation.
    capture = Harness()
    capture_coordinator = capture.coordinator()
    for run_id in RUN_IDS[:2]:
        assert capture_coordinator.start_captured(captured_request(run_id)).accepted
    first_reservation, second_reservation = capture.resident.register_calls
    first_failure = _failed_capture(RUN_IDS[0], first_reservation)
    capture.resident.complete(first_failure)
    capture.resident.complete(first_failure)
    assert [fact.run_id for fact in capture.persistence.terminal_calls] == [
        RUN_IDS[0]
    ]
    assert second_reservation not in capture.resident.cancel_calls
    assert RUN_IDS[1] in capture_coordinator.snapshot().accepted_active_run_ids

    # Failed-capture current cleanup latches restart without changing the claimed
    # terminal or touching the other run's collector.
    cleanup = Harness()
    bad_collector = FakeCurrentCollector(
        cleanup.trace,
        samples=[CurrentSample(0.0, 1.0)],
    )
    sibling_collector = FakeCurrentCollector(
        cleanup.trace,
        samples=[CurrentSample(0.0, 2.0)],
    )
    bad_collector.stop_exception = TimeoutError("current stop timed out")
    cleanup.current_factory.collectors.extend(
        (bad_collector, sibling_collector)
    )
    cleanup.captured_freezer.result_factory = lambda request: frozen_captured(
        request,
        current_enabled=True,
    )
    cleanup_coordinator = cleanup.coordinator()
    for run_id in RUN_IDS[:2]:
        assert cleanup_coordinator.start_captured(captured_request(run_id)).accepted
    cleanup.resident.complete(
        _failed_capture(
            RUN_IDS[0],
            cleanup.resident.register_calls[0],
            "claimed capture failure",
        )
    )
    cleanup_terminal = cleanup.persistence.terminal_calls[0]
    assert cleanup_terminal.error_category is TerminalErrorCategory.CAPTURE_FAILED
    assert cleanup_terminal.reason == "claimed capture failure"
    assert len(bad_collector.stop_calls) == bad_collector.samples_calls == 1
    assert sibling_collector.stop_calls == []
    assert sibling_collector.samples_calls == 0
    assert cleanup_coordinator.snapshot().restart_required is True
    assert cleanup_coordinator.start_captured(
        captured_request(RUN_IDS[2])
    ).status is StartStatus.INGRESS_CLOSED

    # Channel identity mismatch fails before current attach, audio, or analysis.
    channel = Harness()
    channel.current_factory.collectors.extend(
        (
            FakeCurrentCollector(
                channel.trace,
                samples=[CurrentSample(0.0, 1.0)],
            ),
            FakeCurrentCollector(
                channel.trace,
                samples=[CurrentSample(0.0, 2.0)],
            ),
        )
    )
    channel.captured_freezer.result_factory = lambda request: frozen_captured(
        request,
        current_enabled=True,
        channel_order=(1,),
    )
    channel_coordinator = channel.coordinator()
    for run_id in RUN_IDS[:2]:
        assert channel_coordinator.start_captured(captured_request(run_id)).accepted
    channel.resident.complete(
        completed_capture(channel.resident.register_calls[0])
    )
    channel_terminal = channel.persistence.terminal_calls[0]
    assert channel_terminal.run_id == RUN_IDS[0]
    assert channel_terminal.error_category is TerminalErrorCategory.CAPTURE_FAILED
    assert channel_terminal.reason == "channel-order mismatch"
    assert channel.persistence.current_calls == []
    assert channel.save_dispatcher.calls == []
    assert channel.scheduler.initial_calls == []
    assert RUN_IDS[1] in channel_coordinator.snapshot().accepted_active_run_ids

    # A resident hardware stream may contain every physical input channel.
    # Each run saves only its frozen selected channels, in frozen order.
    selected = Harness()
    selected.captured_freezer.result_factory = lambda request: replace(
        frozen_captured(request, channel_order=(1, 3)),
        metadata=AudioMetadataConstraints(
            expected_sample_rate_hz=16_000,
            expected_channel_count=2,
            expected_frame_count=2,
            format=PersistedAudioFormat.PCM16_WAV,
        ),
    )
    selected_coordinator = selected.coordinator()
    assert selected_coordinator.start_captured(
        captured_request(RUN_IDS[0])
    ).accepted
    reservation = selected.resident.register_calls[0]
    physical_audio = CapturedAudioValue(
        source_identity="six-channel-capture",
        sample_rate_hz=16_000,
        channel_count=6,
        frame_count=2,
        format=CapturedSourceFormat.INT16_INTERLEAVED,
        interleaved_bytes=struct.pack(
            "<12h",
            10,
            11,
            12,
            13,
            14,
            15,
            20,
            21,
            22,
            23,
            24,
            25,
        ),
        channel_indices=(0, 1, 2, 3, 4, 5),
    )
    selected.resident.complete(
        CaptureOutcome(
            run_id=RUN_IDS[0],
            reservation=reservation,
            status=CaptureOutcomeStatus.COMPLETED,
            audio=physical_audio,
            error_category=None,
            reason="",
        )
    )
    selected_audio = selected.save_dispatcher.calls[0][0].audio
    assert selected_audio.channel_indices == (1, 3)
    assert selected_audio.channel_count == 2
    assert struct.unpack("<4h", selected_audio.interleaved_bytes) == (
        11,
        13,
        21,
        23,
    )
    assert selected.persistence.terminal_calls == []

    # Audio callback failure has one terminal authority and leaves the sibling live.
    audio = Harness()
    audio_coordinator = audio.coordinator()
    for run_id in RUN_IDS[:2]:
        assert audio_coordinator.start_captured(captured_request(run_id)).accepted
    audio.resident.complete(completed_capture(audio.resident.register_calls[0]))
    save_request, save_sink = audio.save_dispatcher.calls[0]
    audio_failure = AudioSaveOutcome(
        save_job_id=save_request.save_job_id,
        run_id=save_request.run_id,
        source_kind=save_request.source_kind,
        source_identity=save_request.source_identity,
        status=AudioSaveStatus.FAILED,
        persisted_audio=None,
        error_category=TerminalErrorCategory.AUDIO_SAVE_FAILED,
        reason="audio persistence failed",
    )
    save_sink(audio_failure)
    save_sink(audio_failure)
    assert [fact.run_id for fact in audio.persistence.terminal_calls] == [
        RUN_IDS[0]
    ]
    assert RUN_IDS[1] in audio_coordinator.snapshot().accepted_active_run_ids

    # A committed analysis failure is run-local.
    analysis, analysis_coordinator = _start_two_at_bridge()
    analysis.scheduler.complete_report(
        terminal_report(
            RUN_IDS[0],
            ATTEMPT_IDS[0],
            state=AttemptState.FAILED,
            judgment=None,
            result_json=None,
            code="ANALYSIS_EXECUTION_FAILED",
            summary="analysis failed",
        )
    )
    analysis_terminal = next(
        fact
        for fact in analysis.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    )
    assert analysis_terminal.run_id == RUN_IDS[0]
    assert (
        analysis_terminal.projection.error_category
        is TerminalErrorCategory.ANALYSIS_FAILED
    )
    assert analysis_coordinator.snapshot().analysis_attempt_ids == (
        ATTEMPT_IDS[1],
    )

    # Terminal persistence failure emits no history and cannot steal another
    # acquired analysis attempt.
    persistence, persistence_coordinator = _start_two_at_bridge()
    persistence.scheduler.complete_report(
        terminal_report(
            RUN_IDS[0],
            ATTEMPT_IDS[0],
            status=BridgeTerminalStatus.DB_FAILED,
            state=AttemptState.FAILED,
            judgment=None,
            result_json=None,
            code="ATTEMPT_TERMINAL_DB_FAILED",
            summary="database failed",
        )
    )
    assert not [
        fact
        for fact in persistence.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
        and fact.run_id == RUN_IDS[0]
    ]
    assert persistence_coordinator.snapshot().restart_required is True
    assert ATTEMPT_IDS[1] in persistence_coordinator.snapshot().analysis_attempt_ids

    # A wrong result-channel attempt identity quarantines only that run. The exact
    # sibling report still reaches its own terminal projection once.
    result_channel, result_coordinator = _start_two_at_bridge()
    result_channel.scheduler.complete_report(
        terminal_report(RUN_IDS[0], ATTEMPT_IDS[1])
    )
    assert result_coordinator.snapshot().restart_required is True
    assert not [
        fact
        for fact in result_channel.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
        and fact.run_id == RUN_IDS[0]
    ]
    result_channel.scheduler.complete_report(
        terminal_report(RUN_IDS[1], ATTEMPT_IDS[1])
    )
    assert [
        fact.run_id
        for fact in result_channel.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    ] == [RUN_IDS[1]]

    # The selected durable revision is authoritative; stale UI intent performs
    # zero judgment persistence and cannot alter another run's facts.
    stale = Harness()
    stale.persistence.facts_by_run[RUN_IDS[0]] = result_facts(revision=8)
    stale.persistence.facts_by_run[RUN_IDS[1]] = result_facts(
        run_id=RUN_IDS[1],
        revision=4,
    )
    stale_coordinator = stale.coordinator()
    stale_result = stale_coordinator.apply_manual(
        manual_request(RUN_IDS[0], revision=7)
    )
    assert stale_result.status is OperationStatus.STALE_REVISION
    assert stale.persistence.manual_calls == []
    assert stale.persistence.facts_by_run[RUN_IDS[1]].causal_revision == 4

    # Playback rejects a cross-run durable fact before reading audio or starting
    # the backend, leaving an independent valid service usable.
    playback_calls = []
    mismatched_playback = make_playback_service(
        run_repository=FakeRunRepository(
            make_run(run_id=OTHER_RUN_ID),
            calls=playback_calls,
        ),
        audio_repository=FakeAudioRepository(calls=playback_calls),
        audio_loader=FakeAudioLoader(calls=playback_calls),
        backend=FakeBackend(calls=playback_calls),
        monotonic=SequenceClock(10.0, 10.0),
    )
    playback_failure = mismatched_playback.play(playback_request())
    assert playback_failure.code is PlaybackCode.PERSISTED_FACT_INVALID
    assert playback_calls == [("run.get", PLAYBACK_RUN_ID)]
    valid_playback = make_playback_service()
    assert valid_playback.play(playback_request()).code is PlaybackCode.STARTED

    # Every shutdown stage's exact non-clean result is observed once, cached,
    # and never retried.
    shutdown_stages = (
        "begin",
        "bridge_a",
        "finish",
        "producer",
        "plc",
        "plc_owner",
        "capture",
        "resident",
        "playback",
        "writer",
        "config_writer",
        "config_db",
        "backend",
        "bridge_b",
    )
    for stage in shutdown_stages:
        _trace, calls, resources = shutdown_harness(
            overrides={stage: _non_clean_result(stage)}
        )
        owner = ApplicationShutdownOwner(
            resources,
            monotonic=_Clock(),
            timeout_seconds=5.0,
        )
        report = owner.shutdown()
        assert report.clean is False
        assert report.restart_required is True
        assert owner.shutdown() is report
        assert len(calls[stage].arguments) == 1
        assert all(len(call.arguments) <= 1 for call in calls.values())
