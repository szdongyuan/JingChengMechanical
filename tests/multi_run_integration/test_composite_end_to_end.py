from __future__ import annotations

from dataclasses import replace
import json

import pytest

from app.service.multi_run_coordinator_mvp import StartStatus, TriggerSource
from app.service.multi_run_integration.plc_dispatch import PlcDispatchPorts
from app.service.plc_protocol import PLC_NEW_RECORD_DEFAULT_OFFSETS
from app.service.plc_result_sequencer import (
    PlcAttemptOutcome,
    PlcResultAttemptReport,
    PlcResultSequencer,
)
from app.service.plc_service import PlcService
from app.service.run_persistence import Judgment
from tests.multi_run_coordinator_mvp._support import (
    Harness,
    RUN_IDS,
    canonical_payload,
    captured_request,
    completed_capture,
    frozen_captured,
    saved_outcome,
)
from tests.multi_run_coordinator_mvp.test_bridge_initial import terminal_report
from tests.test_plc_service_protocol import BusStub, FakeS7Client


class _RunningThread:
    @staticmethod
    def isRunning() -> bool:
        return True


def _successful_result_json(judgment: str = "PASS") -> str:
    return json.dumps(
        {
            "judgment": judgment,
            "result": {
                "judgment_payload": {
                    "ai_score": 88.75,
                    "channel_results": [
                        {
                            "spl_max_db": 62.25,
                            "loudness_max_sone": 4.5,
                        }
                    ],
                },
                "summary_payload": {
                    "overall_status": judgment.lower(),
                    "rows": [
                        {
                            "item_key": "stft_score",
                            "actual_value": 91.5,
                        }
                    ],
                },
                "chart_payload": {
                    "0": {
                        "vibration": {
                            "overall_rms": 0.125,
                        }
                    }
                },
            },
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@pytest.mark.parametrize(
    ("judgment", "passed"),
    ((Judgment.PASS, True), (Judgment.FAIL, False)),
)
def test_frozen_coordinator_terminal_reaches_real_default_plc_boundary(
    judgment: Judgment,
    passed: bool,
) -> None:
    harness = Harness()
    live_labels = {
        "schema": "jingcheng-history-labels/v1",
        "config": {
            "config_id": 7,
            "name": "Motor A",
            "fault_types": [],
            "details": [],
        },
        "sn": "SN-FROZEN",
        "trigger_source": "plc",
    }
    frozen_labels = canonical_payload(live_labels)
    harness.captured_freezer.result_factory = lambda request: replace(
        frozen_captured(request),
        history_labels=frozen_labels,
    )

    service = PlcService(
        BusStub(),
        lambda *_args: None,
        "127.0.0.1",
        field_offsets_dict=PLC_NEW_RECORD_DEFAULT_OFFSETS,
        plc_id=1,
    )
    client = FakeS7Client()
    service.plc_client = client
    field_writes: list[tuple[str, object]] = []
    write_field_value = service.write_field_value

    def record_write(field_name: str, value: object) -> bool:
        field_writes.append((field_name, value))
        return write_field_value(field_name, value)

    service.write_field_value = record_write
    physical_payloads = []
    write_result = service.write_result

    def record_result(plc_id: int, payload: object) -> None:
        physical_payloads.append(dict(payload))
        write_result(plc_id, payload)

    service.write_result = record_result
    ports = PlcDispatchPorts(
        plc_services={1: service},
        plc_threads={1: _RunningThread()},
    )
    reports = []
    harness.plc = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=ports.write_testing_state,
        report_attempt=reports.append,
    )
    coordinator = harness.coordinator()
    request = captured_request(source=TriggerSource.PLC)

    coordinator.start_captured(request)
    live_labels["sn"] = "SN-EDITED-AFTER-FREEZE"
    live_labels["config"]["name"] = "Edited Motor"
    harness.resident.complete(
        completed_capture(harness.resident.register_calls[0])
    )
    save_request, save_sink = harness.save_dispatcher.calls[0]
    save_sink(saved_outcome(save_request))
    harness.scheduler.complete_report(
        terminal_report(
            request.run_id,
            judgment=judgment,
            result_json=_successful_result_json(judgment.value),
        )
    )

    assert [
        write for write in field_writes
        if write[0] != "res_testing_signal_offset"
    ] == [
        ("res_error_code_offset", 0),
        ("res_result_offset", passed),
        ("res_score_offset", 91.5),
        ("res_spl_offset", 62.25),
        ("res_loudness_offset", 4.5),
        ("res_vibrate_data_offset", 0.125),
        ("res_ai_score_offset", 88.75),
        ("res_finish_signal_offset", True),
    ]
    assert physical_payloads == [
        {
            "finished": True,
            "error_code": 0,
            "sn_code": "SN-FROZEN",
            "test_result": passed,
            "score": 91.5,
            "spl": 62.25,
            "loudness": 4.5,
            "vibrate": 0.125,
            "ai_score": 88.75,
        }
    ]
    assert len([report for report in reports if hasattr(report, "plc_sequence")]) == 1


def test_plc_result_write_failure_is_one_shot_and_does_not_close_ingress() -> None:
    harness = Harness()
    frozen_labels = canonical_payload(
        {
            "schema": "jingcheng-history-labels/v1",
            "config": {
                "config_id": 7,
                "name": "Motor A",
                "fault_types": [],
                "details": [],
            },
            "sn": "SN-FROZEN",
            "trigger_source": "plc",
        }
    )
    harness.captured_freezer.result_factory = lambda request: replace(
        frozen_captured(request),
        history_labels=frozen_labels,
    )
    service = PlcService(
        BusStub(),
        lambda *_args: None,
        "127.0.0.1",
        field_offsets_dict=PLC_NEW_RECORD_DEFAULT_OFFSETS,
        plc_id=1,
    )
    client = FakeS7Client()
    client.write_failures[(14, 4)] = [RuntimeError("score unavailable")]
    service.plc_client = client
    score_attempts = 0
    write_field_value = service.write_field_value

    def count_score_attempt(field_name: str, value: object) -> bool:
        nonlocal score_attempts
        if field_name == "res_score_offset":
            score_attempts += 1
        return write_field_value(field_name, value)

    service.write_field_value = count_score_attempt
    ports = PlcDispatchPorts(
        plc_services={1: service},
        plc_threads={1: _RunningThread()},
    )
    reports = []
    harness.plc = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=ports.write_testing_state,
        report_attempt=reports.append,
    )
    coordinator = harness.coordinator()
    result_json = _successful_result_json()

    first = captured_request(RUN_IDS[0], source=TriggerSource.PLC)
    coordinator.start_captured(first)
    harness.resident.complete(
        completed_capture(harness.resident.register_calls[-1])
    )
    save_request, save_sink = harness.save_dispatcher.calls[-1]
    save_sink(saved_outcome(save_request))
    harness.scheduler.complete_report(
        terminal_report(first.run_id, result_json=result_json)
    )

    terminal_event = harness.history.calls[-1]
    assert terminal_event.projection.result_payload is not None
    assert terminal_event.projection.result_payload.canonical_bytes == (
        result_json.encode("utf-8")
    )
    assert coordinator.snapshot().restart_required is False
    failed_reports = [
        report
        for report in reports
        if isinstance(report, PlcResultAttemptReport)
        and report.outcome is PlcAttemptOutcome.BOUNDARY_FAILED
    ]
    assert len(failed_reports) == 1
    assert failed_reports[0].field_name == "res_score_offset"
    assert score_attempts == 1

    second = captured_request(RUN_IDS[1], source=TriggerSource.PLC)
    assert coordinator.start_captured(second).status is StartStatus.ACCEPTED_ACTIVE
    harness.resident.complete(
        completed_capture(harness.resident.register_calls[-1])
    )
    save_request, save_sink = harness.save_dispatcher.calls[-1]
    save_sink(saved_outcome(save_request))
    harness.scheduler.complete_report(
        terminal_report(second.run_id, result_json=result_json)
    )

    assert score_attempts == 2
    assert coordinator.snapshot().restart_required is False
    assert any(
        isinstance(report, PlcResultAttemptReport)
        and report.run_id == second.run_id
        and report.outcome is PlcAttemptOutcome.BOUNDARY_RETURNED
        for report in reports
    )


def test_terminal_plc_publication_exception_does_not_latch_global_restart() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    request = captured_request(RUN_IDS[0], source=TriggerSource.PLC)
    harness.plc.publish_exception = RuntimeError("PLC publication failed")

    coordinator.start_captured(request)
    harness.resident.complete(
        completed_capture(harness.resident.register_calls[-1])
    )
    save_request, save_sink = harness.save_dispatcher.calls[-1]
    save_sink(saved_outcome(save_request))
    harness.scheduler.complete_report(terminal_report(request.run_id))

    assert coordinator.snapshot().restart_required is False
    assert coordinator.snapshot().pending_publication_run_ids == ()
    assert (
        coordinator.start_captured(
            captured_request(RUN_IDS[1], source=TriggerSource.MANUAL)
        ).status
        is StartStatus.ACCEPTED_ACTIVE
    )
