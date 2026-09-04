from __future__ import annotations

import pytest

from app.service.run_record_export.contracts import (
    AiExportValue,
    AudioExportFacts,
    CurrentExportFacts,
    RunDataExportCounts,
    RunDataExportOptions,
    RunDataExportReadReport,
    RunDataExportRecord,
    RunDataExportResult,
)


RUN_ID = "3e8c5a8e-0ec9-47f8-ae4b-55461024f552"


def _report() -> RunDataExportReadReport:
    record = RunDataExportRecord(
        run_id=RUN_ID,
        selection_index=0,
        accepted_at="2026-07-28T01:02:03.000000Z",
        terminal_at="2026-07-28T01:02:04.000000Z",
        sn="SN-PINNED-001",
        model="MODEL-PINNED",
        state="SUCCEEDED",
        terminal_code="ANALYSIS_SUCCEEDED",
        judgment="PASS",
        judgment_source="AUTOMATIC",
        fault_type=None,
        audio=AudioExportFacts(None, None, None, None, None, "not available"),
        current=CurrentExportFacts(None, "not available"),
    )
    return RunDataExportReadReport((RUN_ID,), (record,), ())


class _Reader:
    def __init__(self, report: RunDataExportReadReport) -> None:
        self.report = report
        self.report_calls: list[tuple[str, ...]] = []
        self.report_cancel_checks: list[object] = []
        self.copy_calls: list[tuple[tuple[str, ...], object]] = []

    def read_configurable_report(
        self, run_ids: tuple[str, ...], *, cancel_check=None
    ):
        self.report_calls.append(run_ids)
        self.report_cancel_checks.append(cancel_check)
        return self.report

    def read_copy_database(self, run_ids, *, cancel_check=None):
        self.copy_calls.append((run_ids, cancel_check))
        return b"database"

    def pending_enrichment_run_ids(self, _run_ids):
        raise AssertionError("configurable export must not await enrichment")


class _AiReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def get_current_effective_export_values(self, run_ids):
        self.calls.append(run_ids)
        return {RUN_ID: AiExportValue(0.75, "raw-label")}


def _result(path: str, options: RunDataExportOptions) -> RunDataExportResult:
    return RunDataExportResult(
        path,
        options.output_kind,
        RunDataExportCounts(1, 1, 0, 0, 0, 0, 0, 0),
    )


def test_service_orchestrates_v2_reads_and_passes_report_intact(monkeypatch, tmp_path):
    import app.service.run_record_export.archive as archive_module

    report = _report()
    reader = _Reader(report)
    ai_reader = _AiReader()
    options = RunDataExportOptions(True, False, True, False)
    target = str(tmp_path / "selected.zip")
    progress_events = []
    cancel_calls = []
    package_calls = []

    def package(received_report, received_target, **kwargs):
        package_calls.append((received_report, received_target, kwargs))
        kwargs["progress_callback"](0, 5, "package")
        kwargs["progress_callback"](5, 5, "done")
        return _result(received_target, options)

    monkeypatch.setattr(archive_module, "write_configurable_package", package)
    service = archive_module.RunArchiveService(reader, ai_reader)
    result = service.export(
        (RUN_ID,),
        target,
        options=options,
        progress_callback=lambda *event: progress_events.append(event),
        cancel_check=lambda: cancel_calls.append("check"),
    )

    assert result.counts.exported_runs == 1
    assert reader.report_calls == [(RUN_ID,)]
    assert reader.report_cancel_checks == [package_calls[0][2]["cancel_check"]]
    assert ai_reader.calls == [(RUN_ID,)]
    assert reader.copy_calls[0][0] == (RUN_ID,)
    assert reader.copy_calls[0][1] is package_calls[0][2]["cancel_check"]
    received_report, received_target, kwargs = package_calls[0]
    assert received_report is report
    assert received_target == target
    assert kwargs["ai_values"] == {RUN_ID: AiExportValue(0.75, "raw-label")}
    assert kwargs["options"] is options
    assert kwargs["database_bytes"] == b"database"
    assert len(cancel_calls) >= 6
    totals = [event[1] for event in progress_events]
    assert totals[:3] == [0, 0, 0]
    determinate = [event for event in progress_events if event[1] > 0]
    assert [event[0] for event in determinate] == [0, 5]


def test_service_checks_cancel_immediately_after_read_progress(monkeypatch, tmp_path):
    import app.service.run_record_export.archive as archive_module

    reader = _Reader(_report())
    ai_reader = _AiReader()
    armed = False

    def progress(_completed, _total, _message):
        nonlocal armed
        armed = True

    def cancel():
        if armed:
            raise RuntimeError("cancel after read progress")

    monkeypatch.setattr(
        archive_module,
        "write_configurable_package",
        lambda *_args, **_kwargs: pytest.fail("package must not run"),
    )

    with pytest.raises(RuntimeError, match="cancel after read progress"):
        archive_module.RunArchiveService(reader, ai_reader).export(
            (RUN_ID,),
            str(tmp_path / "selected.xlsx"),
            options=RunDataExportOptions(True, False, False, False),
            progress_callback=progress,
            cancel_check=cancel,
        )

    assert reader.report_calls == []


def test_service_skips_database_read_when_not_selected(monkeypatch, tmp_path):
    import app.service.run_record_export.archive as archive_module

    reader = _Reader(_report())
    ai_reader = _AiReader()
    options = RunDataExportOptions(True, False, False, False)
    monkeypatch.setattr(
        archive_module,
        "write_configurable_package",
        lambda report, target, **_kwargs: _result(target, options),
    )

    archive_module.RunArchiveService(reader, ai_reader).export(
        (RUN_ID,), str(tmp_path / "selected.xlsx"), options=options
    )

    assert reader.copy_calls == []
    assert ai_reader.calls == [(RUN_ID,)]


@pytest.mark.parametrize("stop_at", [1, 2, 3, 4, 5])
def test_service_cancellation_around_run_and_ai_reads_stops_later_work(
    monkeypatch, tmp_path, stop_at
):
    import app.service.run_record_export.archive as archive_module

    reader = _Reader(_report())
    ai_reader = _AiReader()
    package_calls = []
    monkeypatch.setattr(
        archive_module,
        "write_configurable_package",
        lambda *_args, **_kwargs: package_calls.append(True),
    )
    checks = 0

    def cancel():
        nonlocal checks
        checks += 1
        if checks == stop_at:
            raise RuntimeError(f"cancel {stop_at}")

    with pytest.raises(RuntimeError, match=f"cancel {stop_at}"):
        archive_module.RunArchiveService(reader, ai_reader).export(
            (RUN_ID,),
            str(tmp_path / "selected.xlsx"),
            options=RunDataExportOptions(True, False, False, False),
            cancel_check=cancel,
        )

    assert package_calls == []
    assert len(reader.report_calls) == (1 if stop_at >= 3 else 0)
    assert len(ai_reader.calls) == (1 if stop_at == 5 else 0)


def test_database_failure_preserves_target_and_creates_no_temporary_file(
    monkeypatch, tmp_path
):
    import app.service.run_record_export.archive as archive_module

    reader = _Reader(_report())
    ai_reader = _AiReader()
    target = tmp_path / "selected.zip"
    target.write_bytes(b"existing")

    def fail_database(*_args, **_kwargs):
        raise RuntimeError("database failure")

    reader.read_copy_database = fail_database
    monkeypatch.setattr(
        archive_module,
        "write_configurable_package",
        lambda *_args, **_kwargs: pytest.fail("package must not run"),
    )

    with pytest.raises(RuntimeError, match="database failure"):
        archive_module.RunArchiveService(reader, ai_reader).export(
            (RUN_ID,),
            str(target),
            options=RunDataExportOptions(False, False, True, False),
        )

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []


def test_database_cancellation_preserves_target_and_never_starts_package(
    monkeypatch, tmp_path
):
    import app.service.run_record_export.archive as archive_module

    reader = _Reader(_report())
    ai_reader = _AiReader()
    target = tmp_path / "selected.zip"
    target.write_bytes(b"existing")

    def canceling_database(_run_ids, *, cancel_check):
        cancel_check()
        raise AssertionError("cancel check should have stopped the read")

    reader.read_copy_database = canceling_database
    monkeypatch.setattr(
        archive_module,
        "write_configurable_package",
        lambda *_args, **_kwargs: pytest.fail("package must not run"),
    )
    checks = 0

    def cancel():
        nonlocal checks
        checks += 1
        if checks == 7:
            raise RuntimeError("database canceled")

    with pytest.raises(RuntimeError, match="database canceled"):
        archive_module.RunArchiveService(reader, ai_reader).export(
            (RUN_ID,),
            str(target),
            options=RunDataExportOptions(False, False, True, False),
            cancel_check=cancel,
        )

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []
