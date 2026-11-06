"""Child-only real Qt/kernel probe; never import this module in the pytest host."""
from __future__ import annotations

import ctypes
import faulthandler
import json
import os
from pathlib import Path
import sqlite3
import sys
from threading import Event
import time
import traceback
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressDialog
import numpy as np
from scipy.io import wavfile

from test_data_management_module import _module, _page, _row, _select_all_rows
from app.service.run_record_export import (
    AudioExportFacts, CurrentExportFacts, RunDataExportOptions,
    RunDataExportReadReport, RunDataExportRecord,
)
from app.service.run_record_export import package
import ui.modules.data_management as ui


def main(directory: Path, count: int, mode: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fault_log = (directory / "fault.log").open("w", encoding="utf-8")
    faulthandler.enable(fault_log)
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    report_path = directory / "report.json"
    app = QApplication([])
    options = RunDataExportOptions(True, mode == "zip", mode == "zip", mode == "zip")
    target = directory / ("export." + options.output_kind)
    if mode in {"cancel", "failure"}:
        target.write_bytes(b"existing target")
    wav = directory / "source.wav"
    wavfile.write(wav, 8000, np.zeros(8, dtype=np.int16))
    database = directory / "fixture.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE selected_runs (run_id TEXT PRIMARY KEY)")
        ids = tuple(str(UUID(int=i + 1, version=4)) for i in range(count))
        conn.executemany("INSERT INTO selected_runs VALUES (?)", ((x,) for x in ids))
    records = tuple(RunDataExportRecord(
        run_id=run_id, selection_index=i,
        accepted_at="2026-07-29T01:02:03.000000Z",
        terminal_at="2026-07-29T01:02:04.000000Z", sn=f"SN-{i:04d}",
        model="MODEL-A", state="SUCCEEDED", terminal_code="ANALYSIS_SUCCEEDED",
        judgment="PASS", judgment_source="AUTOMATIC", fault_type=None,
        audio=AudioExportFacts(str(wav), 8000, 1, 8, (0,), None),
        current=CurrentExportFacts(((0.0, 1.0), (0.1, 2.0)), None),
    ) for i, run_id in enumerate(ids))
    queued, consumed = Event(), Event()
    burst_count = 0
    depth = maximum_depth = 0
    calls = 0
    kernel_ids = []
    messages = []
    callbacks = []
    burst_limit = count + 1  # initial preparation plus one checkpoint per record

    class ArchiveService:
        def export(self, selected_ids, path, *, options, progress_callback, cancel_check):
            assert tuple(selected_ids) == ids
            kernel_ids.extend(record.run_id for record in records)

            def checkpoint(completed, total, message):
                nonlocal burst_count
                if mode == "cancel" and message == "正在写入 Excel...":
                    module._export_worker.request_cancel()
                progress_callback(completed, total, message)
                if burst_count < burst_limit:
                    burst_count += 1
                    queued.set()
                    assert consumed.wait(10), "GUI did not consume checkpoint"
                    consumed.clear()
                else:
                    queued.set()

            return package.write_run_data_export(
                RunDataExportReadReport(ids, records, ()), path,
                ai_values={}, options=options,
                database_bytes=database.read_bytes() if options.include_database else None,
                progress_callback=checkpoint, cancel_check=cancel_check,
            )

    module, management, _ = _module(app, ArchiveService())
    management.next_page = _page(tuple(_row(x, sn=f"SN-{i:04d}") for i, x in enumerate(ids[:50])), total_count=count, total_pages=(count + 49) // 50)
    module._request_and_render(1)
    _select_all_rows(module)
    module._selected_run_id_set.update(ids)
    controls = [(control, control.isEnabled()) for control in module._operation_controls()]
    ui.choose_run_export_options = lambda *args, **kwargs: options
    ui.choose_run_export_path = lambda *args, **kwargs: str(target)
    QMessageBox.information = lambda _parent, title, text: messages.append((title, text))
    QMessageBox.warning = lambda _parent, title, text: messages.append((title, text))
    if mode == "failure":
        def fail_replace(*args, **kwargs):
            raise OSError("injected publication failure")
        package.os.replace = fail_replace

    def profile(frame, event, arg):
        nonlocal depth, maximum_depth, calls
        if frame.f_code.co_name == "_export_selected_rows" and event == "return":
            callbacks.append(frame.f_locals["update_progress"])
        if frame.f_code.co_name != "update_progress" or Path(frame.f_code.co_filename) != ROOT / "ui/modules/data_management.py":
            return
        if event == "call":
            depth += 1
            calls += 1
            maximum_depth = max(maximum_depth, depth)
            if depth > 32:
                stack = "".join(traceback.format_stack(frame))
                report_path.write_text(json.dumps({"max_depth": maximum_depth, "stack": stack}), encoding="utf-8")
                fault_log.write(stack)
                fault_log.flush()
                os._exit(86)  # fail safely before a platform-dependent native stack overflow
            if calls <= burst_limit:
                queued.clear()
                consumed.set()
                assert queued.wait(10), "worker did not queue next checkpoint"
        elif event == "return":
            depth -= 1

    sys.setprofile(profile)
    module._export_selected_rows()
    progress = module._export_progress
    assert isinstance(progress, QProgressDialog)
    assert queued.wait(10), "worker did not start"
    deadline = time.monotonic() + 60
    while module._operation_active or module._export_worker is not None:
        assert time.monotonic() < deadline, "export did not finish"
        app.processEvents()
    app.processEvents()
    assert all(control.isEnabled() == before for control, before in controls)
    assert module._export_progress is None
    assert kernel_ids == list(ids)
    assert not list(directory.glob(".run-export-*"))
    expected_title = {"cancel": "导出已取消", "failure": "导出失败"}.get(mode, "导出完成")
    assert len(messages) == 1 and messages[0][0] == expected_title, messages
    if mode in {"cancel", "failure"}:
        assert target.read_bytes() == b"existing target"
    # Exercise a new real QThread/dialog after the first thread has finished.
    # Retain its closure only through profiling; do not replace any signal slot.
    second_export = mode == "xlsx" and count == 1
    if second_export:
        old_snapshot = (progress.value(), progress.labelText())
        callbacks[0](999, 999, "late after success")
        assert (progress.value(), progress.labelText()) == old_snapshot
        module._export_selected_rows()
        replacement = module._export_progress
        snapshot = (replacement.value(), replacement.labelText())
        callbacks[0](999, 999, "late during subsequent export")
        assert (replacement.value(), replacement.labelText()) == snapshot
        assert (progress.value(), progress.labelText()) == old_snapshot
        deadline = time.monotonic() + 30
        while module._operation_active or module._export_worker is not None:
            assert time.monotonic() < deadline, "second export did not finish"
            app.processEvents()
        assert len(messages) == 2 and messages[-1][0] == "导出完成"
        assert kernel_ids == list(ids) * 2
        assert module._export_progress is None
        assert all(control.isEnabled() == before for control, before in controls)
    sys.setprofile(None)
    report_path.write_text(json.dumps({
        "count": count, "max_depth": maximum_depth, "calls": calls,
        "status": expected_title, "worker_finished": module._export_worker is None,
        "controls_restored": True, "kernel_ids": kernel_ids[:count],
        "second_export": second_export,
    }), encoding="utf-8")
    module.close()
    fault_log.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3])
