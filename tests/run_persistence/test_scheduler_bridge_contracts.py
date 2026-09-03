from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
import inspect
import struct
import textwrap
from typing import get_type_hints

import pytest

from app.service.analysis.scheduler import (
    AiToolchainSnapshot,
    AnalysisBundleIdentity,
    AnalysisCancelOrigin,
    AnalysisInputSnapshot,
    AnalysisJob,
    AnalysisKind,
    AudioStorageKind,
    CancelReceipt,
    CancelStatus,
    FrozenResultObject,
    PersistedAudioReference,
    ShutdownReport,
    ShutdownStatus,
    SubmitReceipt,
    SubmitStatus,
)
from app.service.run_persistence import (
    AttemptInsert,
    AttemptKind,
    AttemptState,
    DatabaseLifecycleCode,
    Judgment,
    EnrichmentCommittedEvent,
    TransactionResult,
    TransactionState,
    freeze_analysis_envelope,
)
from app.service.run_persistence.audio_repository import AudioRecord
from app.service.run_persistence.run_repository import RunRecord
from app.service.run_persistence.run_persistence_scheduler_bridge import (
    AnalysisPersistenceBoundary,
    AnalysisResultDecodeError,
    AnalysisSchedulerPort,
    BridgeAuthorityStatus,
    BridgeCancelRequest,
    BridgeCancelResult,
    BridgeCancelStatus,
    BridgeDatabaseCloseResult,
    BridgeDatabaseCloseStatus,
    BridgeSchedulerShutdownResult,
    BridgeSchedulerShutdownStatus,
    BridgeSubmissionResult,
    BridgeSubmissionStatus,
    BridgeTerminalReport,
    BridgeTerminalStatus,
    CoordinatorAnalysisTerminalReportSink,
    DecodedAnalysisResult,
    InitialAnalysisRequest,
    PreparedAnalysisFacts,
    PreparedAnalysisSubmission,
    ReanalysisRequest,
    build_prevalidated_submission,
    decode_analysis_result_payload,
    prepare_analysis_facts,
)
from app.service.run_persistence import run_persistence_scheduler_bridge as bridge_module


RUN_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"
AUDIO_ID = "33333333-3333-4333-8333-333333333333"
NOW = "2026-07-27T12:34:56.123456Z"

_PRE_ALLOCATION_NOT_ACQUIRED_CODES = (
    "ANALYSIS_ADMISSION_CLOSED",
    "ANALYSIS_DB_CLOSED",
    "ANALYSIS_RUN_NOT_FOUND",
    "ANALYSIS_RUN_NOT_ACTIVE",
    "REANALYSIS_RUN_NOT_FOUND",
    "REANALYSIS_RUN_NOT_TERMINAL",
    "ANALYSIS_AUDIO_NOT_FOUND",
    "ANALYSIS_AUDIO_IDENTITY_MISMATCH",
    "REANALYSIS_AUDIO_UNREADABLE",
    "ANALYSIS_ENVELOPE_INVALID",
    "ANALYSIS_KIND_INVALID",
    "ANALYSIS_QUEUE_TIMESTAMP_FAILED",
)


def _audio_reference(
    storage_kind: AudioStorageKind = AudioStorageKind.CAPTURED_FINAL_WAV,
) -> PersistedAudioReference:
    return PersistedAudioReference(
        reference_id=AUDIO_ID,
        storage_kind=storage_kind,
        absolute_path=r"C:\managed\audio.wav",
        sample_rate_hz=48_000,
        channel_count=2,
        frame_count=144_000,
        dtype="<i2",
        content_revision=f"{AUDIO_ID}:1024",
        audio_data_id=AUDIO_ID,
    )


def _accepted_receipt() -> SubmitReceipt:
    return SubmitReceipt(
        status=SubmitStatus.ACCEPTED,
        run_id=RUN_ID,
        analysis_attempt_id=ATTEMPT_ID,
        submission_sequence=1,
        error=None,
    )


def _cancel_receipt(status: CancelStatus) -> CancelReceipt:
    return CancelReceipt(
        status=status,
        run_id=RUN_ID,
        analysis_attempt_id=ATTEMPT_ID,
        drain_pending=status is CancelStatus.CANCEL_REQUESTED_RUNNING,
        error=None,
    )


def _shutdown_report(status: ShutdownStatus) -> ShutdownReport:
    remaining = status is not ShutdownStatus.CLOSED
    return ShutdownReport(
        status=status,
        started_monotonic=1.0,
        finished_monotonic=2.0,
        canceled_pending=0,
        cancel_requested_running=1 if remaining else 0,
        remaining_attempt_ids=(ATTEMPT_ID,) if remaining else (),
        remaining_worker_ids=(1,) if remaining else (),
        remaining_run_lanes=(RUN_ID,) if remaining else (),
        remaining_toolchain_lanes=(),
        control_thread_alive=remaining,
    )


def _committed(value: bool) -> TransactionResult[bool]:
    return TransactionResult(
        state=TransactionState.COMMITTED,
        value=value,
    )


def _terminal_report(
    *,
    status: BridgeTerminalStatus = BridgeTerminalStatus.COMMITTED,
    persistence_result: TransactionResult[bool] | None = None,
    restart_required: bool = False,
) -> BridgeTerminalReport:
    if persistence_result is None and status is BridgeTerminalStatus.COMMITTED:
        persistence_result = _committed(True)
    return BridgeTerminalReport(
        status=status,
        run_id=RUN_ID,
        analysis_attempt_id=ATTEMPT_ID,
        attempt_kind=AttemptKind.INITIAL,
        attempt_state=AttemptState.SUCCEEDED,
        judgment=Judgment.PASS,
        result_payload_json='{"judgment":"PASS","result":{}}',
        code="ANALYSIS_SUCCEEDED",
        summary=None,
        terminal_at=NOW,
        persistence_result=persistence_result,
        restart_required=restart_required,
    )


def test_request_values_are_frozen_exact_and_validate_identity() -> None:
    initial = InitialAnalysisRequest(RUN_ID, _audio_reference())
    selected_start = struct.unpack("!d", bytes.fromhex("3fd3333333333333"))[0]
    selected_end = struct.unpack("!d", bytes.fromhex("400199999999999a"))[0]
    reanalysis = ReanalysisRequest(RUN_ID, selected_start, selected_end)
    cancel = BridgeCancelRequest(
        RUN_ID,
        ATTEMPT_ID,
        AnalysisCancelOrigin.USER_STOP,
        "operator stopped analysis",
    )

    with pytest.raises(FrozenInstanceError):
        initial.run_id = ATTEMPT_ID  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        reanalysis.analysis_window_start_sec = selected_end  # type: ignore[misc]
    assert initial.audio_reference.reference_id == AUDIO_ID
    assert reanalysis.run_id == RUN_ID
    assert reanalysis.analysis_window_start_sec == selected_start
    assert reanalysis.analysis_window_end_sec == selected_end
    assert struct.pack("!d", reanalysis.analysis_window_start_sec) == struct.pack(
        "!d", selected_start
    )
    assert struct.pack("!d", reanalysis.analysis_window_end_sec) == struct.pack(
        "!d", selected_end
    )
    assert not hasattr(reanalysis, "__dict__")
    assert cancel.origin is AnalysisCancelOrigin.USER_STOP

    with pytest.raises((TypeError, ValueError)):
        InitialAnalysisRequest("not-a-uuid", _audio_reference())
    with pytest.raises(TypeError):
        InitialAnalysisRequest(RUN_ID, object())  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        ReanalysisRequest("NOT-CANONICAL", selected_start, selected_end)
    with pytest.raises(TypeError):
        BridgeCancelRequest(
            RUN_ID,
            ATTEMPT_ID,
            "user_stop",  # type: ignore[arg-type]
            "reason",
        )
    with pytest.raises(ValueError):
        BridgeCancelRequest(
            RUN_ID,
            ATTEMPT_ID,
            AnalysisCancelOrigin.USER_STOP,
            "",
        )
    with pytest.raises(ValueError):
        BridgeCancelRequest(
            RUN_ID,
            ATTEMPT_ID,
            AnalysisCancelOrigin.USER_STOP,
            "界" * 171,
        )


def test_enrichment_committed_event_is_frozen_and_carries_terminal_revision() -> None:
    identity = AnalysisBundleIdentity(
        run_id=RUN_ID,
        analysis_attempt_id=ATTEMPT_ID,
        content_revision=f"{AUDIO_ID}:1024",
        algorithm_revision="algorithm-1",
        config_identity="config-1",
        calibration_identity="calibration-1",
        channel_mapping_identity="channels-1",
        window_start_seconds=0.0,
        window_end_seconds=3.0,
    )
    event = EnrichmentCommittedEvent(
        identity=identity,
        judgment=Judgment.PASS,
        result_payload_json='{"judgment":"PASS","result":{}}',
        terminal_revision="a" * 64,
        committed_at=NOW,
    )

    assert tuple(field.name for field in fields(event)) == (
        "identity",
        "judgment",
        "result_payload_json",
        "terminal_revision",
        "committed_at",
    )
    with pytest.raises(FrozenInstanceError):
        event.committed_at = NOW  # type: ignore[misc]


def test_reanalysis_request_has_exact_selected_window_fields() -> None:
    assert tuple(field.name for field in fields(ReanalysisRequest)) == (
        "run_id",
        "analysis_window_start_sec",
        "analysis_window_end_sec",
    )
    assert tuple(field.name for field in fields(InitialAnalysisRequest)) == (
        "run_id",
        "audio_reference",
    )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("analysis_window_start_sec", True),
        ("analysis_window_start_sec", 0),
        ("analysis_window_start_sec", "0.25"),
        ("analysis_window_start_sec", Decimal("0.25")),
        ("analysis_window_end_sec", False),
        ("analysis_window_end_sec", 2),
        ("analysis_window_end_sec", "2.25"),
        ("analysis_window_end_sec", Decimal("2.25")),
    ],
)
def test_reanalysis_window_rejects_non_exact_float_types(
    field_name: str,
    value: object,
) -> None:
    values = {
        "run_id": RUN_ID,
        "analysis_window_start_sec": 0.25,
        "analysis_window_end_sec": 2.25,
    }
    values[field_name] = value

    with pytest.raises(TypeError):
        ReanalysisRequest(**values)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (float("nan"), 2.0),
        (0.0, float("nan")),
        (float("inf"), 2.0),
        (float("-inf"), 2.0),
        (0.0, float("inf")),
        (0.0, float("-inf")),
        (-0.25, 2.0),
        (1.0, 1.0),
        (2.0, 1.0),
    ],
)
def test_reanalysis_window_rejects_invalid_float_values_and_order(
    start: float,
    end: float,
) -> None:
    with pytest.raises(ValueError):
        ReanalysisRequest(RUN_ID, start, end)


def test_selected_window_path_static_audit_has_only_exact_forwarding() -> None:
    path_functions = (
        bridge_module._validate_selected_analysis_window,
        bridge_module.RunPersistenceSchedulerBridge.submit_reanalysis,
        bridge_module.RunPersistenceSchedulerBridge._submit,
        bridge_module.prepare_analysis_facts,
    )
    sources = tuple(inspect.getsource(function) for function in path_functions)
    combined_source = "\n".join(sources)

    for forbidden_call in (
        "float(",
        "round(",
        "min(",
        "max(",
        "json.dumps",
        "json.loads",
    ):
        assert forbidden_call not in combined_source
    arithmetic_nodes = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)
    for source in sources:
        syntax = ast.parse(textwrap.dedent(source))
        assert not any(
            isinstance(node, ast.BinOp)
            and isinstance(node.op, arithmetic_nodes)
            for node in ast.walk(syntax)
        )

    submit_source = sources[1]
    internal_submit_source = sources[2]
    mapper_source = sources[3]
    assert (
        "analysis_window_start_sec=request.analysis_window_start_sec"
        in submit_source
    )
    assert (
        "analysis_window_end_sec=request.analysis_window_end_sec"
        in submit_source
    )
    assert (
        "analysis_window_start_sec=analysis_window_start_sec"
        in internal_submit_source
    )
    assert (
        "analysis_window_end_sec=analysis_window_end_sec"
        in internal_submit_source
    )
    assert "snapshot_window_start = envelope.window_start_seconds" in mapper_source
    assert "snapshot_window_end = envelope.window_end_seconds" in mapper_source
    assert "snapshot_window_start = analysis_window_start_sec" in mapper_source
    assert "snapshot_window_end = analysis_window_end_sec" in mapper_source
    assert (
        "analysis_window_start_seconds=snapshot_window_start"
        in mapper_source
    )
    assert "analysis_window_end_seconds=snapshot_window_end" in mapper_source


def test_protocols_expose_only_the_exact_scheduler_and_callback_shapes() -> None:
    scheduler_hints = {
        name: get_type_hints(getattr(AnalysisSchedulerPort, name))
        for name in ("submit", "cancel", "shutdown")
    }
    callback_hints = get_type_hints(
        CoordinatorAnalysisTerminalReportSink.__call__
    )

    assert set(scheduler_hints["submit"]) == {"job", "return"}
    assert set(scheduler_hints["cancel"]) == {
        "run_id",
        "analysis_attempt_id",
        "origin",
        "reason",
        "return",
    }
    assert set(scheduler_hints["shutdown"]) == {
        "timeout_seconds",
        "return",
    }
    assert callback_hints == {
        "report": BridgeTerminalReport,
        "return": type(None),
    }


def _submission_row(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": BridgeSubmissionStatus.ACCEPTED,
        "authority": BridgeAuthorityStatus.ACQUIRED,
        "run_id": RUN_ID,
        "analysis_attempt_id": ATTEMPT_ID,
        "scheduler_accepted": True,
        "terminal_delivered_before_return": False,
        "restart_required": False,
        "code": None,
        "reason": None,
        "submit_receipt": _accepted_receipt(),
    }
    return base | changes


def _nonaccepted_receipt(
    status: SubmitStatus = SubmitStatus.QUEUE_FULL,
) -> SubmitReceipt:
    category = {
        SubmitStatus.INVALID_JOB:
            bridge_module.AnalysisErrorCategory.INVALID_JOB,
        SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID:
            bridge_module.AnalysisErrorCategory.DUPLICATE_ANALYSIS_ATTEMPT_ID,
        SubmitStatus.QUEUE_FULL:
            bridge_module.AnalysisErrorCategory.ANALYSIS_QUEUE_FULL,
        SubmitStatus.NOT_ACCEPTING:
            bridge_module.AnalysisErrorCategory.SCHEDULER_NOT_ACCEPTING,
    }[status]
    return SubmitReceipt(
        status=status,
        run_id=RUN_ID,
        analysis_attempt_id=ATTEMPT_ID,
        submission_sequence=None,
        error=bridge_module.AnalysisError(
            category=category,
            stage=bridge_module.AnalysisStage.ADMISSION,
            summary=status.value,
        ),
    )


def test_submission_authority_and_result_have_exact_closed_shape() -> None:
    assert tuple(BridgeAuthorityStatus) == (
        BridgeAuthorityStatus.NOT_ACQUIRED,
        BridgeAuthorityStatus.ACQUIRED,
        BridgeAuthorityStatus.UNKNOWN,
    )
    assert [field.name for field in fields(BridgeSubmissionResult)] == [
        "status",
        "authority",
        "run_id",
        "analysis_attempt_id",
        "scheduler_accepted",
        "terminal_delivered_before_return",
        "restart_required",
        "code",
        "reason",
        "submit_receipt",
    ]
    assert "terminal_report" not in inspect.signature(
        BridgeSubmissionResult
    ).parameters


@pytest.mark.parametrize(
    "row",
    [
        _submission_row(),
        _submission_row(
            status=BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL,
            terminal_delivered_before_return=True,
        ),
        _submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            analysis_attempt_id=None,
            scheduler_accepted=False,
            code="ANALYSIS_ADMISSION_CLOSED",
            submit_receipt=None,
        ),
        _submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            scheduler_accepted=False,
            restart_required=True,
            code="DATABASE_CLOSED",
            submit_receipt=None,
        ),
        _submission_row(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            authority=BridgeAuthorityStatus.UNKNOWN,
            scheduler_accepted=False,
            restart_required=True,
            code="ATTEMPT_QUEUE_CALL_FAILED",
            submit_receipt=None,
        ),
        _submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            scheduler_accepted=False,
            terminal_delivered_before_return=True,
            code="ANALYSIS_QUEUE_FULL",
            submit_receipt=_nonaccepted_receipt(),
        ),
        _submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            scheduler_accepted=False,
            terminal_delivered_before_return=True,
            code="SCHEDULER_RECEIPT_INVALID",
            submit_receipt=None,
        ),
        _submission_row(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            scheduler_accepted=True,
            terminal_delivered_before_return=True,
            restart_required=True,
            code="ATTEMPT_TERMINAL_DB_FAILED",
            reason="write failed",
        ),
        _submission_row(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            scheduler_accepted=False,
            terminal_delivered_before_return=True,
            restart_required=True,
            code="ATTEMPT_TERMINAL_DB_FAILED",
            reason="write failed",
            submit_receipt=_nonaccepted_receipt(),
        ),
    ],
)
def test_every_legal_submission_result_matrix_row_constructs(
    row: dict[str, object],
) -> None:
    BridgeSubmissionResult(**row)


@pytest.mark.parametrize("code", _PRE_ALLOCATION_NOT_ACQUIRED_CODES)
def test_every_exact_preallocation_not_acquired_code_constructs(
    code: str,
) -> None:
    BridgeSubmissionResult(
        **_submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            analysis_attempt_id=None,
            scheduler_accepted=False,
            restart_required=code == "ANALYSIS_DB_CLOSED",
            code=code,
            submit_receipt=None,
        )
    )


@pytest.mark.parametrize("attempt_id", [None, ATTEMPT_ID])
@pytest.mark.parametrize(
    "code",
    tuple(code.value for code in DatabaseLifecycleCode),
)
def test_every_database_lifecycle_not_acquired_allocation_shape_constructs(
    code: str,
    attempt_id: str | None,
) -> None:
    BridgeSubmissionResult(
        **_submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            analysis_attempt_id=attempt_id,
            scheduler_accepted=False,
            restart_required=True,
            code=code,
            reason="database unavailable",
            submit_receipt=None,
        )
    )


def test_allocated_queue_rollback_not_acquired_row_constructs() -> None:
    BridgeSubmissionResult(
        **_submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            scheduler_accepted=False,
            code="ATTEMPT_QUEUE_DB_FAILED",
            reason="queue transaction rolled back",
            submit_receipt=None,
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {
            "analysis_attempt_id": None,
            "code": "TOTALLY_UNSPECIFIED_CODE",
        },
        {
            "analysis_attempt_id": None,
            "code": "TOTALLY_UNSPECIFIED_CODE",
            "reason": "arbitrary reason",
        },
        {
            "analysis_attempt_id": None,
            "code": "ANALYSIS_RUN_NOT_FOUND",
            "reason": "arbitrary reason",
        },
        {
            "analysis_attempt_id": None,
            "restart_required": True,
            "code": "ANALYSIS_DB_CLOSED",
            "reason": "arbitrary reason",
        },
        {
            "code": "ANALYSIS_JOB_INVALID",
            "reason": "arbitrary reason",
        },
        {
            "code": "ATTEMPT_QUEUE_DB_FAILED",
            "reason": None,
        },
    ],
)
def test_not_acquired_rejects_unlisted_codes_and_illegal_reasons(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        BridgeSubmissionResult(
            **_submission_row(
                status=BridgeSubmissionStatus.REJECTED,
                authority=BridgeAuthorityStatus.NOT_ACQUIRED,
                scheduler_accepted=False,
                submit_receipt=None,
                **changes,
            )
        )


@pytest.mark.parametrize(
    "replacement",
    [
        {"status": "ACCEPTED"},
        {"authority": "ACQUIRED"},
        {"analysis_attempt_id": None},
        {"scheduler_accepted": False},
        {"terminal_delivered_before_return": True},
        {"restart_required": True},
        {"code": "UNEXPECTED"},
        {"reason": "unexpected"},
        {"submit_receipt": None},
    ],
)
def test_accepted_submission_rejects_every_single_field_mutation(
    replacement: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        BridgeSubmissionResult(**_submission_row(**replacement))


@pytest.mark.parametrize(
    "row",
    [
        _submission_row(
            status=BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL,
            terminal_delivered_before_return=False,
        ),
        _submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.UNKNOWN,
            scheduler_accepted=False,
            code="REJECTED",
            submit_receipt=None,
        ),
        _submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            analysis_attempt_id=None,
            scheduler_accepted=False,
            code="ANALYSIS_JOB_INVALID",
            submit_receipt=None,
        ),
        _submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            analysis_attempt_id=ATTEMPT_ID,
            scheduler_accepted=False,
            code="ANALYSIS_RUN_NOT_FOUND",
            submit_receipt=None,
        ),
        _submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            analysis_attempt_id=None,
            scheduler_accepted=False,
            restart_required=True,
            code="ANALYSIS_RUN_NOT_FOUND",
            submit_receipt=None,
        ),
        _submission_row(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            authority=BridgeAuthorityStatus.UNKNOWN,
            scheduler_accepted=False,
            terminal_delivered_before_return=True,
            restart_required=True,
            code="UNKNOWN",
            submit_receipt=None,
        ),
        _submission_row(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            authority=BridgeAuthorityStatus.UNKNOWN,
            scheduler_accepted=True,
            restart_required=True,
            code="UNKNOWN",
        ),
        _submission_row(
            status=BridgeSubmissionStatus.REJECTED,
            scheduler_accepted=False,
            terminal_delivered_before_return=True,
            code="REJECTED",
            submit_receipt=_accepted_receipt(),
        ),
    ],
)
def test_submission_result_rejects_meaningful_cross_field_mutations(
    row: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        BridgeSubmissionResult(**row)


@pytest.mark.parametrize(
    ("receipt_status", "wrong_code"),
    [
        (SubmitStatus.INVALID_JOB, "ANALYSIS_QUEUE_FULL"),
        (
            SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID,
            "ANALYSIS_SCHEDULER_NOT_ACCEPTING",
        ),
        (SubmitStatus.QUEUE_FULL, "ANALYSIS_INVALID_JOB"),
        (SubmitStatus.NOT_ACCEPTING, "ANALYSIS_DUPLICATE_ATTEMPT_ID"),
    ],
)
def test_acquired_rejection_requires_exact_receipt_status_code_mapping(
    receipt_status: SubmitStatus,
    wrong_code: str,
) -> None:
    with pytest.raises(ValueError):
        BridgeSubmissionResult(
            **_submission_row(
                status=BridgeSubmissionStatus.REJECTED,
                scheduler_accepted=False,
                terminal_delivered_before_return=True,
                code=wrong_code,
                submit_receipt=_nonaccepted_receipt(receipt_status),
            )
        )


@pytest.mark.parametrize(
    "changes",
    [
        {
            "code": "ANALYSIS_QUEUE_FULL",
            "submit_receipt": None,
        },
        {
            "code": "SCHEDULER_RECEIPT_INVALID",
            "reason": "arbitrary reason",
            "submit_receipt": None,
        },
        {
            "code": "ANALYSIS_QUEUE_FULL",
            "reason": "arbitrary reason",
            "submit_receipt": _nonaccepted_receipt(),
        },
    ],
)
def test_acquired_committed_rejection_rejects_illegal_code_or_reason(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        BridgeSubmissionResult(
            **_submission_row(
                status=BridgeSubmissionStatus.REJECTED,
                scheduler_accepted=False,
                terminal_delivered_before_return=True,
                **changes,
            )
        )


@pytest.mark.parametrize(
    "changes",
    [
        {
            "scheduler_accepted": True,
            "code": "ANALYSIS_ADMISSION_CLOSED",
            "submit_receipt": _accepted_receipt(),
        },
        {
            "scheduler_accepted": True,
            "code": "ATTEMPT_TERMINAL_RESULT_INVALID",
            "reason": "arbitrary reason",
            "submit_receipt": _accepted_receipt(),
        },
        {
            "scheduler_accepted": False,
            "code": "ANALYSIS_INVALID_JOB",
            "submit_receipt": _nonaccepted_receipt(SubmitStatus.INVALID_JOB),
        },
        {
            "scheduler_accepted": False,
            "code": "UNSUPPORTED_SCHEDULER_OUTCOME",
            "submit_receipt": _nonaccepted_receipt(),
        },
    ],
)
def test_acquired_noncommitted_rows_reject_nonterminal_codes_and_reasons(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        BridgeSubmissionResult(
            **_submission_row(
                status=BridgeSubmissionStatus.STATE_UNKNOWN,
                terminal_delivered_before_return=True,
                restart_required=True,
                **changes,
            )
        )


def test_boundary_and_submit_signatures_require_one_per_submit_sink() -> None:
    assert list(inspect.signature(AnalysisPersistenceBoundary.__init__).parameters) == [
        "self",
        "runtime",
        "attempt_repository",
        "now_utc",
        "enrichment_sink",
    ]
    assert list(
        inspect.signature(bridge_module.RunPersistenceSchedulerBridge.submit_initial).parameters
    ) == ["self", "request", "terminal_sink"]
    assert list(
        inspect.signature(
            bridge_module.RunPersistenceSchedulerBridge.submit_reanalysis
        ).parameters
    ) == ["self", "request", "terminal_sink"]


def test_public_package_exports_authority_and_hides_private_prepared_values() -> None:
    import app.service.run_persistence as package

    assert package.BridgeAuthorityStatus is BridgeAuthorityStatus
    assert "BridgeAuthorityStatus" in package.__all__
    for name in ("PreparedAnalysisFacts", "PreparedAnalysisSubmission"):
        assert name not in package.__all__
        assert not hasattr(package, name)


@pytest.mark.parametrize(
    ("bridge_status", "scheduler_status"),
    [
        (BridgeCancelStatus.CANCEL_REQUESTED, CancelStatus.CANCELED_PENDING),
        (
            BridgeCancelStatus.CANCEL_REQUESTED,
            CancelStatus.CANCEL_REQUESTED_RUNNING,
        ),
        (BridgeCancelStatus.ALREADY_TERMINAL, CancelStatus.ALREADY_TERMINAL),
    ],
)
def test_cancel_result_preserves_exact_origin_and_receipt_mapping(
    bridge_status: BridgeCancelStatus,
    scheduler_status: CancelStatus,
) -> None:
    receipt = _cancel_receipt(scheduler_status)
    result = BridgeCancelResult(
        status=bridge_status,
        run_id=RUN_ID,
        analysis_attempt_id=ATTEMPT_ID,
        origin=AnalysisCancelOrigin.USER_STOP,
        code=None,
        reason=None,
        receipt=receipt,
    )
    assert result.receipt is receipt


def test_cancel_result_rejects_local_enums_mismatches_and_illegal_optionals() -> None:
    with pytest.raises(TypeError):
        BridgeCancelResult(
            status="CANCEL_REQUESTED",  # type: ignore[arg-type]
            run_id=RUN_ID,
            analysis_attempt_id=ATTEMPT_ID,
            origin=AnalysisCancelOrigin.USER_STOP,
            code=None,
            reason=None,
            receipt=_cancel_receipt(CancelStatus.CANCELED_PENDING),
        )
    with pytest.raises(ValueError):
        BridgeCancelResult(
            status=BridgeCancelStatus.CANCEL_REQUESTED,
            run_id=RUN_ID,
            analysis_attempt_id=ATTEMPT_ID,
            origin=AnalysisCancelOrigin.USER_STOP,
            code="UNEXPECTED",
            reason=None,
            receipt=_cancel_receipt(CancelStatus.CANCELED_PENDING),
        )
    with pytest.raises(ValueError):
        BridgeCancelResult(
            status=BridgeCancelStatus.ALREADY_TERMINAL,
            run_id=RUN_ID,
            analysis_attempt_id=ATTEMPT_ID,
            origin=AnalysisCancelOrigin.USER_STOP,
            code=None,
            reason=None,
            receipt=_cancel_receipt(CancelStatus.CANCELED_PENDING),
        )
    with pytest.raises(ValueError):
        BridgeCancelResult(
            status=BridgeCancelStatus.REJECTED,
            run_id=RUN_ID,
            analysis_attempt_id=ATTEMPT_ID,
            origin=AnalysisCancelOrigin.USER_STOP,
            code=None,
            reason="bad receipt",
            receipt=None,
        )


@pytest.mark.parametrize(
    ("status", "persistence_result", "restart_required"),
    [
        (BridgeTerminalStatus.COMMITTED, _committed(True), False),
        (BridgeTerminalStatus.LATE_IGNORED, _committed(False), False),
        (
            BridgeTerminalStatus.DB_FAILED,
            TransactionResult(
                state=TransactionState.ROLLED_BACK,
                reason="write failed",
            ),
            False,
        ),
        (
            BridgeTerminalStatus.DB_UNKNOWN,
            TransactionResult(
                state=TransactionState.UNKNOWN,
                reason="state unknown",
                restart_required=True,
            ),
            True,
        ),
    ],
)
def test_terminal_report_transaction_coherence(
    status: BridgeTerminalStatus,
    persistence_result: TransactionResult[bool],
    restart_required: bool,
) -> None:
    report = _terminal_report(
        status=status,
        persistence_result=persistence_result,
        restart_required=restart_required,
    )
    assert report.persistence_result is persistence_result


def test_terminal_report_rejects_malformed_nested_results_and_payload_coherence() -> None:
    invalid_results = (
        TransactionResult(
            state=TransactionState.COMMITTED,
            value=1,
        ),
        TransactionResult(
            state=TransactionState.COMMITTED,
            value=True,
            reason="impossible",
        ),
        TransactionResult(
            state=TransactionState.ROLLED_BACK,
            value=False,
            reason="failed",
        ),
        TransactionResult(
            state=TransactionState.UNKNOWN,
            reason="unknown",
            restart_required=False,
        ),
    )
    for invalid in invalid_results:
        with pytest.raises((TypeError, ValueError)):
            _terminal_report(persistence_result=invalid)

    with pytest.raises(ValueError):
        BridgeTerminalReport(
            status=BridgeTerminalStatus.COMMITTED,
            run_id=RUN_ID,
            analysis_attempt_id=ATTEMPT_ID,
            attempt_kind=AttemptKind.INITIAL,
            attempt_state=AttemptState.FAILED,
            judgment=Judgment.PASS,
            result_payload_json='{"judgment":"PASS","result":{}}',
            code="FAILED",
            summary=None,
            terminal_at=NOW,
            persistence_result=_committed(True),
            restart_required=False,
        )
    with pytest.raises(ValueError):
        _terminal_report(
            status=BridgeTerminalStatus.DB_UNKNOWN,
            persistence_result=TransactionResult(
                state=TransactionState.UNKNOWN,
                reason="unknown",
                restart_required=True,
            ),
            restart_required=False,
        )
    with pytest.raises(TypeError):
        BridgeTerminalReport(
            status=BridgeTerminalStatus.UNSUPPORTED,
            run_id=RUN_ID,
            analysis_attempt_id=ATTEMPT_ID,
            attempt_kind="INITIAL",  # type: ignore[arg-type]
            attempt_state=None,
            judgment=None,
            result_payload_json=None,
            code="UNSUPPORTED_OUTCOME",
            summary=None,
            terminal_at=None,
            persistence_result=None,
            restart_required=False,
        )


@pytest.mark.parametrize(
    "result_payload_json",
    [
        "[]",
        '{"judgment":"PASS"}',
        '{"judgment":"PASS","payload":{}}',
        '{"extra":null,"judgment":"PASS","result":{}}',
        '{"judgment":"PASS","judgment":"PASS","result":{}}',
        '{"result":{},"judgment":"PASS"}',
        '{"judgment": "PASS", "result": {}}',
    ],
)
def test_successful_terminal_report_requires_exact_canonical_full_root_json(
    result_payload_json: str,
) -> None:
    with pytest.raises(ValueError):
        replace(
            _terminal_report(),
            result_payload_json=result_payload_json,
        )


def test_successful_terminal_report_requires_matching_root_judgment() -> None:
    with pytest.raises(ValueError):
        replace(
            _terminal_report(),
            result_payload_json='{"judgment":"FAIL","result":{}}',
        )


@pytest.mark.parametrize(
    ("bridge_status", "scheduler_status", "code"),
    [
        (
            BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED,
            ShutdownStatus.CLOSED,
            None,
        ),
        (
            BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS,
            ShutdownStatus.IN_PROGRESS,
            "SCHEDULER_IN_PROGRESS",
        ),
        (
            BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS,
            ShutdownStatus.IN_PROGRESS,
            "BRIDGE_SUBMIT_IN_FLIGHT",
        ),
        (
            BridgeSchedulerShutdownStatus.SCHEDULER_INCOMPLETE,
            ShutdownStatus.SHUTDOWN_INCOMPLETE,
            "SCHEDULER_INCOMPLETE",
        ),
    ],
)
def test_scheduler_shutdown_result_exact_mapping(
    bridge_status: BridgeSchedulerShutdownStatus,
    scheduler_status: ShutdownStatus,
    code: str | None,
) -> None:
    result = BridgeSchedulerShutdownResult(
        status=bridge_status,
        scheduler_report=_shutdown_report(scheduler_status),
        code=code,
    )
    assert result.scheduler_report.status is scheduler_status


@pytest.mark.parametrize(
    ("status", "database_closed", "restart_required", "code"),
    [
        (BridgeDatabaseCloseStatus.DATABASE_CLOSED, True, False, None),
        (
            BridgeDatabaseCloseStatus.DATABASE_ALREADY_CLOSED,
            True,
            True,
            "DATABASE_QUARANTINED",
        ),
        (
            BridgeDatabaseCloseStatus.BLOCKED,
            False,
            False,
            "SCHEDULER_NOT_CLOSED",
        ),
    ],
)
def test_database_close_result_exact_mapping(
    status: BridgeDatabaseCloseStatus,
    database_closed: bool,
    restart_required: bool,
    code: str | None,
) -> None:
    result = BridgeDatabaseCloseResult(
        status=status,
        database_closed=database_closed,
        restart_required=restart_required,
        code=code,
    )
    assert result.status is status


def test_shutdown_and_database_results_reject_incoherent_values() -> None:
    with pytest.raises(ValueError):
        BridgeSchedulerShutdownResult(
            status=BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED,
            scheduler_report=_shutdown_report(ShutdownStatus.IN_PROGRESS),
            code=None,
        )
    with pytest.raises(ValueError):
        BridgeDatabaseCloseResult(
            status=BridgeDatabaseCloseStatus.DATABASE_CLOSED,
            database_closed=False,
            restart_required=False,
            code=None,
        )
    with pytest.raises(ValueError):
        BridgeDatabaseCloseResult(
            status=BridgeDatabaseCloseStatus.BLOCKED,
            database_closed=False,
            restart_required=True,
            code="SCHEDULER_NOT_CLOSED",
        )


@pytest.mark.parametrize("judgment", ["PASS", "FAIL"])
@pytest.mark.parametrize("reverse_root_order", [False, True])
def test_decode_payload_returns_persistence_judgment_and_canonical_full_root(
    judgment: str,
    reverse_root_order: bool,
) -> None:
    nested = FrozenResultObject(
        items=(
            ("中文", "值"),
            ("scalars", (None, True, 7, 1.25, "text")),
        )
    )
    items = (
        (("result", nested), ("judgment", judgment))
        if reverse_root_order
        else (("judgment", judgment), ("result", nested))
    )

    decoded = decode_analysis_result_payload(FrozenResultObject(items=items))

    assert decoded == DecodedAnalysisResult(
        judgment=Judgment(judgment),
        result_payload_json=(
            '{"judgment":"'
            + judgment
            + '","result":{"scalars":[null,true,7,1.25,"text"],"中文":"值"}}'
        ),
    )
    with pytest.raises(FrozenInstanceError):
        decoded.result_payload_json = "{}"  # type: ignore[misc]


def _forge_frozen_object(
    items: object,
) -> FrozenResultObject:
    value = object.__new__(FrozenResultObject)
    object.__setattr__(value, "items", items)
    return value


@pytest.mark.parametrize(
    "payload",
    [
        None,
        (),
        "not-an-object",
        FrozenResultObject(items=(("judgment", "PASS"),)),
        FrozenResultObject(
            items=(
                ("judgment", "PASS"),
                ("result", None),
                ("extra", None),
            )
        ),
        FrozenResultObject(
            items=(("judgment", "UNKNOWN"), ("result", None))
        ),
        FrozenResultObject(items=(("judgment", True), ("result", None))),
        _forge_frozen_object(
            (
                ("judgment", "PASS"),
                ("judgment", "FAIL"),
                ("result", None),
            )
        ),
        _forge_frozen_object(
            (("judgment", "PASS"), ("result", ["mutable"]))
        ),
        _forge_frozen_object(
            (("judgment", "PASS"), ("result", float("nan")))
        ),
        _forge_frozen_object(
            (("judgment", "PASS"), ("result", ("x" * 65_537,)))
        ),
    ],
)
def test_decode_payload_rejects_every_invalid_frozen_payload_as_typed_error(
    payload: object,
) -> None:
    with pytest.raises(AnalysisResultDecodeError):
        decode_analysis_result_payload(payload)  # type: ignore[arg-type]


def test_decode_payload_translates_canonical_serialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.service.run_persistence.run_persistence_scheduler_bridge as bridge_module

    payload = FrozenResultObject(
        items=(("judgment", "PASS"), ("result", None))
    )

    def fail_serialization(*args: object, **kwargs: object) -> str:
        raise OverflowError("synthetic JSON failure")

    monkeypatch.setattr(bridge_module.json, "dumps", fail_serialization)

    with pytest.raises(
        AnalysisResultDecodeError,
        match="canonical serialization failed",
    ):
        decode_analysis_result_payload(payload)


def _envelope_json(*, with_toolchain: bool = False) -> str:
    toolchain = None
    if with_toolchain:
        toolchain = {
            "toolchain_id": " acoustic-v2 ",
            "execution_revision": "exec-r7",
            "execution_mode": "subprocess",
            "entrypoint": "analyze.py",
            "package_dir": r"C:\toolchains\acoustic",
            "params": {"z": [2, 1], "中文": "值"},
            "timeout_seconds": 12.5,
        }
    return freeze_analysis_envelope(
        {
            "schema": "analysis-input/v1",
            "config": {"z": 2, "a": {"k": "配置"}},
            "workorder": {"part": "P-7", "batch": 3},
            "channel_mapping": {"right": 1, "left": 0},
            "analysis": {
                "window_start_seconds": 0.25,
                "window_end_seconds": 2.75,
                "algorithm_revision": "algorithm-r4",
                "execution_timeout_seconds": 30.5,
                "queue_timeout_seconds": 4.25,
                "toolchain": toolchain,
            },
        }
    )


def _run_record(
    *,
    ingress_kind: str = "CAPTURED",
    state: str = "ACTIVE",
    with_toolchain: bool = False,
) -> RunRecord:
    terminal = state != "ACTIVE"
    return RunRecord(
        run_id=RUN_ID,
        ingress_kind=ingress_kind,
        trigger_source="PLC" if ingress_kind == "CAPTURED" else "IMPORT",
        config_id=7,
        hardware_id=9 if ingress_kind == "CAPTURED" else None,
        imported_source_identity=(
            "sha256:scheduler-bridge-contracts-import"
            if ingress_kind == "IMPORTED"
            else None
        ),
        imported_source_path=(
            r"C:\managed\scheduler-bridge-contracts-import.wav"
            if ingress_kind == "IMPORTED"
            else None
        ),
        imported_expected_frame_count=(
            144_000 if ingress_kind == "IMPORTED" else None
        ),
        causal_revision=0,
        config_snapshot_json=_envelope_json(with_toolchain=with_toolchain),
        hardware_snapshot_json='{"must_not_enter_scheduler":"hardware"}',
        calibration_snapshot_json='{"z":2,"a":{"offset":1}}',
        state=state,
        judgment="PASS" if state == "SUCCEEDED" else None,
        judgment_source="AUTOMATIC" if state == "SUCCEEDED" else "NONE",
        judged_at=NOW if state == "SUCCEEDED" else None,
        terminal_code="ANALYSIS_SUCCEEDED" if terminal else None,
        terminal_summary=None,
        accepted_at=NOW,
        terminal_at=NOW if terminal else None,
        updated_at=NOW,
    )


def _audio_record(
    *, source_kind: str = "CAPTURED_AUDIO"
) -> AudioRecord:
    return AudioRecord(
        audio_id=AUDIO_ID,
        save_job_id="11111111-1111-4111-8111-111111111111",
        run_id=RUN_ID,
        source_kind=source_kind,
        source_identity=(
            "sha256:scheduler-bridge-contracts-import"
            if source_kind == "IMPORTED_SOURCE"
            else (
                "managed-input:scheduler-bridge-contracts"
                if source_kind == "CAPTURED_AUDIO"
                else "invalid:scheduler-bridge-contracts"
            )
        ),
        content_revision=1,
        final_path=r"C:\managed\audio.wav",
        container_format="WAV_PCM",
        sample_rate_hz=48_000,
        channel_count=2,
        frame_count=144_000,
        duration_ms=3_000,
        byte_size=576_044,
        closed_verified_at=NOW,
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("ingress", "attempt_kind", "state", "scheduler_kind", "storage_kind"),
    [
        (
            "CAPTURED",
            AttemptKind.INITIAL,
            "ACTIVE",
            AnalysisKind.AUTOMATIC,
            AudioStorageKind.CAPTURED_FINAL_WAV,
        ),
        (
            "IMPORTED",
            AttemptKind.INITIAL,
            "ACTIVE",
            AnalysisKind.IMPORTED,
            AudioStorageKind.IMPORTED_FINAL_AUDIO,
        ),
        (
            "CAPTURED",
            AttemptKind.REANALYSIS,
            "SUCCEEDED",
            AnalysisKind.REANALYSIS,
            AudioStorageKind.CAPTURED_FINAL_WAV,
        ),
        (
            "IMPORTED",
            AttemptKind.REANALYSIS,
            "FAILED",
            AnalysisKind.REANALYSIS,
            AudioStorageKind.IMPORTED_FINAL_AUDIO,
        ),
    ],
)
def test_prepare_analysis_facts_maps_closed_durable_kind_and_audio_reference(
    ingress: str,
    attempt_kind: AttemptKind,
    state: str,
    scheduler_kind: AnalysisKind,
    storage_kind: AudioStorageKind,
) -> None:
    facts = prepare_analysis_facts(
        _run_record(ingress_kind=ingress, state=state),
        _audio_record(
            source_kind=(
                "IMPORTED_SOURCE"
                if ingress == "IMPORTED"
                else "CAPTURED_AUDIO"
            )
        ),
        attempt_kind,
        **(
            {
                "analysis_window_start_sec": 0.5,
                "analysis_window_end_sec": 2.5,
            }
            if attempt_kind is AttemptKind.REANALYSIS
            else {}
        ),
    )

    assert type(facts) is PreparedAnalysisFacts
    assert facts.scheduler_kind is scheduler_kind
    assert facts.audio_reference == PersistedAudioReference(
        reference_id=AUDIO_ID,
        storage_kind=storage_kind,
        absolute_path=r"C:\managed\audio.wav",
        sample_rate_hz=48_000,
        channel_count=2,
        frame_count=144_000,
        dtype="<i2",
        content_revision=f"{AUDIO_ID}:576044",
        audio_data_id=AUDIO_ID,
    )
    assert facts.execution_timeout_seconds == 30.5
    assert facts.queue_timeout_seconds == 4.25


def test_prepare_analysis_facts_maps_exact_selected_reanalysis_window() -> None:
    selected_start = struct.unpack("!d", bytes.fromhex("3fd3333333333333"))[0]
    selected_end = struct.unpack("!d", bytes.fromhex("400199999999999a"))[0]

    facts = prepare_analysis_facts(
        _run_record(state="SUCCEEDED"),
        _audio_record(),
        AttemptKind.REANALYSIS,
        analysis_window_start_sec=selected_start,
        analysis_window_end_sec=selected_end,
    )

    assert selected_start != 0.25
    assert selected_end != 2.75
    assert facts.snapshot.analysis_window_start_seconds == selected_start
    assert facts.snapshot.analysis_window_end_seconds == selected_end
    assert struct.pack(
        "!d", facts.snapshot.analysis_window_start_seconds
    ) == struct.pack("!d", selected_start)
    assert struct.pack(
        "!d", facts.snapshot.analysis_window_end_seconds
    ) == struct.pack("!d", selected_end)
    assert (
        facts.snapshot.config_snapshot_json
        == '{"a":{"k":"配置"},"z":2}'
    )
    assert (
        facts.snapshot.calibration_snapshot_json
        == '{"a":{"offset":1},"z":2}'
    )
    assert facts.audio_reference == PersistedAudioReference(
        reference_id=AUDIO_ID,
        storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
        absolute_path=r"C:\managed\audio.wav",
        sample_rate_hz=48_000,
        channel_count=2,
        frame_count=144_000,
        dtype="<i2",
        content_revision=f"{AUDIO_ID}:576044",
        audio_data_id=AUDIO_ID,
    )


@pytest.mark.parametrize(
    "selected_arguments",
    [
        {},
        {"analysis_window_start_sec": 0.5},
        {"analysis_window_end_sec": 2.5},
        {
            "analysis_window_start_sec": True,
            "analysis_window_end_sec": 2.5,
        },
        {
            "analysis_window_start_sec": 0.5,
            "analysis_window_end_sec": Decimal("2.5"),
        },
        {
            "analysis_window_start_sec": float("nan"),
            "analysis_window_end_sec": 2.5,
        },
        {
            "analysis_window_start_sec": 0.5,
            "analysis_window_end_sec": float("inf"),
        },
        {
            "analysis_window_start_sec": -0.5,
            "analysis_window_end_sec": 2.5,
        },
        {
            "analysis_window_start_sec": 2.5,
            "analysis_window_end_sec": 2.5,
        },
        {
            "analysis_window_start_sec": 3.0,
            "analysis_window_end_sec": 2.5,
        },
    ],
)
def test_prepare_analysis_facts_rejects_invalid_selected_reanalysis_matrix(
    selected_arguments: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        prepare_analysis_facts(
            _run_record(state="SUCCEEDED"),
            _audio_record(),
            AttemptKind.REANALYSIS,
            **selected_arguments,
        )


@pytest.mark.parametrize(
    "selected_arguments",
    [
        {
            "analysis_window_start_sec": 0.5,
            "analysis_window_end_sec": None,
        },
        {
            "analysis_window_start_sec": None,
            "analysis_window_end_sec": 2.5,
        },
        {
            "analysis_window_start_sec": 0.5,
            "analysis_window_end_sec": 2.5,
        },
    ],
)
def test_prepare_analysis_facts_rejects_selected_window_for_initial(
    selected_arguments: dict[str, float | None],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        prepare_analysis_facts(
            _run_record(),
            _audio_record(),
            AttemptKind.INITIAL,
            **selected_arguments,
        )


def test_prepared_snapshot_is_canonical_and_excludes_hardware_provenance() -> None:
    facts = prepare_analysis_facts(
        _run_record(with_toolchain=True),
        _audio_record(),
        AttemptKind.INITIAL,
    )

    assert type(facts.snapshot) is AnalysisInputSnapshot
    assert facts.snapshot.config_snapshot_json == '{"a":{"k":"配置"},"z":2}'
    assert facts.snapshot.workorder_snapshot_json == '{"batch":3,"part":"P-7"}'
    assert facts.snapshot.channel_mapping_json == '{"left":0,"right":1}'
    assert facts.snapshot.calibration_snapshot_json == (
        '{"a":{"offset":1},"z":2}'
    )
    assert "hardware" not in repr(facts.snapshot)
    assert "must_not_enter_scheduler" not in (
        facts.snapshot.config_snapshot_json
        + facts.snapshot.calibration_snapshot_json
        + facts.snapshot.workorder_snapshot_json
        + facts.snapshot.channel_mapping_json
    )
    assert facts.snapshot.analysis_window_start_seconds == 0.25
    assert facts.snapshot.analysis_window_end_seconds == 2.75
    assert facts.snapshot.algorithm_revision == "algorithm-r4"
    assert facts.snapshot.ai_toolchain == AiToolchainSnapshot(
        toolchain_id=" acoustic-v2 ",
        runtime_lane_key="ai-toolchain:acoustic-v2",
        execution_revision="exec-r7",
        execution_mode="subprocess",
        entrypoint="analyze.py",
        package_dir=r"C:\toolchains\acoustic",
        params_json='{"z":[2,1],"中文":"值"}',
        timeout_seconds=12.5,
    )


def test_build_prevalidated_submission_returns_exact_immutable_attempt_and_job() -> None:
    facts = prepare_analysis_facts(
        _run_record(),
        _audio_record(),
        AttemptKind.INITIAL,
    )

    prepared = build_prevalidated_submission(
        facts,
        attempt_id=ATTEMPT_ID,
        queued_at=NOW,
    )

    assert type(prepared) is PreparedAnalysisSubmission
    assert type(prepared.attempt_insert) is AttemptInsert
    assert prepared.attempt_insert == AttemptInsert(
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        audio_id=AUDIO_ID,
        attempt_kind=AttemptKind.INITIAL,
        queued_at=NOW,
    )
    assert type(prepared.job) is AnalysisJob
    assert prepared.job == AnalysisJob(
        run_id=RUN_ID,
        analysis_attempt_id=ATTEMPT_ID,
        kind=AnalysisKind.AUTOMATIC,
        audio=facts.audio_reference,
        snapshot=facts.snapshot,
        execution_timeout_seconds=30.5,
        queue_timeout_seconds=4.25,
    )
    assert prepared.job.audio is facts.audio_reference
    assert prepared.job.snapshot is facts.snapshot
    with pytest.raises(FrozenInstanceError):
        prepared.job = prepared.job  # type: ignore[misc]


_INVALID_ANALYSIS_RELATIONSHIPS = (
    (
        AttemptKind.INITIAL,
        AnalysisKind.AUTOMATIC,
        AudioStorageKind.IMPORTED_FINAL_AUDIO,
    ),
    (
        AttemptKind.INITIAL,
        AnalysisKind.IMPORTED,
        AudioStorageKind.CAPTURED_FINAL_WAV,
    ),
    (
        AttemptKind.INITIAL,
        AnalysisKind.REANALYSIS,
        AudioStorageKind.CAPTURED_FINAL_WAV,
    ),
    (
        AttemptKind.INITIAL,
        AnalysisKind.REANALYSIS,
        AudioStorageKind.IMPORTED_FINAL_AUDIO,
    ),
    (
        AttemptKind.REANALYSIS,
        AnalysisKind.AUTOMATIC,
        AudioStorageKind.CAPTURED_FINAL_WAV,
    ),
    (
        AttemptKind.REANALYSIS,
        AnalysisKind.AUTOMATIC,
        AudioStorageKind.IMPORTED_FINAL_AUDIO,
    ),
    (
        AttemptKind.REANALYSIS,
        AnalysisKind.IMPORTED,
        AudioStorageKind.CAPTURED_FINAL_WAV,
    ),
    (
        AttemptKind.REANALYSIS,
        AnalysisKind.IMPORTED,
        AudioStorageKind.IMPORTED_FINAL_AUDIO,
    ),
)


@pytest.mark.parametrize(
    ("attempt_kind", "scheduler_kind", "storage_kind"),
    _INVALID_ANALYSIS_RELATIONSHIPS,
)
def test_prepared_analysis_facts_rejects_every_kind_storage_mismatch(
    attempt_kind: AttemptKind,
    scheduler_kind: AnalysisKind,
    storage_kind: AudioStorageKind,
) -> None:
    facts = prepare_analysis_facts(
        _run_record(),
        _audio_record(),
        AttemptKind.INITIAL,
    )

    with pytest.raises(ValueError):
        replace(
            facts,
            attempt_kind=attempt_kind,
            scheduler_kind=scheduler_kind,
            audio_reference=replace(
                facts.audio_reference,
                storage_kind=storage_kind,
            ),
        )


@pytest.mark.parametrize(
    ("attempt_kind", "scheduler_kind", "storage_kind"),
    _INVALID_ANALYSIS_RELATIONSHIPS,
)
def test_prepared_submission_defensively_rejects_every_kind_storage_mismatch(
    attempt_kind: AttemptKind,
    scheduler_kind: AnalysisKind,
    storage_kind: AudioStorageKind,
) -> None:
    valid = build_prevalidated_submission(
        prepare_analysis_facts(
            _run_record(),
            _audio_record(),
            AttemptKind.INITIAL,
        ),
        attempt_id=ATTEMPT_ID,
        queued_at=NOW,
    )

    with pytest.raises(ValueError):
        PreparedAnalysisSubmission(
            attempt_insert=replace(
                valid.attempt_insert,
                attempt_kind=attempt_kind,
            ),
            job=replace(
                valid.job,
                kind=scheduler_kind,
                audio=replace(
                    valid.job.audio,
                    storage_kind=storage_kind,
                ),
            ),
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("execution_timeout_seconds", float("nan")),
        ("execution_timeout_seconds", float("inf")),
        ("execution_timeout_seconds", float("-inf")),
        ("queue_timeout_seconds", float("nan")),
        ("queue_timeout_seconds", float("inf")),
        ("queue_timeout_seconds", float("-inf")),
    ],
)
def test_prepared_analysis_facts_rejects_nonfinite_timeouts(
    field_name: str,
    value: float,
) -> None:
    facts = prepare_analysis_facts(
        _run_record(),
        _audio_record(),
        AttemptKind.INITIAL,
    )

    with pytest.raises(ValueError):
        replace(facts, **{field_name: value})


@pytest.mark.parametrize(
    ("run", "audio", "attempt_kind"),
    [
        (_run_record(state="SUCCEEDED"), _audio_record(), AttemptKind.INITIAL),
        (_run_record(state="ACTIVE"), _audio_record(), AttemptKind.REANALYSIS),
        (_run_record(ingress_kind="OTHER"), _audio_record(), AttemptKind.INITIAL),
        (
            _run_record(),
            replace(_audio_record(), run_id=ATTEMPT_ID),
            AttemptKind.INITIAL,
        ),
    ],
)
def test_prepare_analysis_facts_rejects_illegal_durable_combinations(
    run: RunRecord,
    audio: AudioRecord,
    attempt_kind: AttemptKind,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        prepare_analysis_facts(run, audio, attempt_kind)
