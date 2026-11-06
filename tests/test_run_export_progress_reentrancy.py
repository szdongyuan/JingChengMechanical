from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import openpyxl
import pytest


def run_probe(directory, count, mode="xlsx"):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "tests/helpers/run_export_progress_probe.py"),
         str(directory), str(count), mode],
        cwd=root, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    report_path = directory / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    assert result.returncode == 0, (result.returncode, report, result.stdout, result.stderr)
    assert report["worker_finished"] and report["controls_restored"]
    return report


def workbook_rows(source, count):
    book = openpyxl.load_workbook(source, read_only=True, data_only=True)
    rows = list(book.active.values)
    book.close()
    assert len(rows) == count + 1
    assert len({row[2] for row in rows[1:]}) == count
    assert {row[2] for row in rows[1:]} == {f"SN-{i:04d}" for i in range(count)}
    return rows


def test_real_export_progress_depth_stays_bounded_as_selection_grows(tmp_path):
    depths = []
    for count in (1, 450, 1000):
        directory = tmp_path / str(count)
        report = run_probe(directory, count)
        assert len(set(report["kernel_ids"])) == count
        workbook_rows(directory / "export.xlsx", count)
        depths.append(report["max_depth"])
    assert max(depths) <= 4, depths  # allow bounded event processing inside Qt
    assert depths[-1] <= depths[1] + 1, depths


def test_same_450_records_export_all_options_with_matching_excel(tmp_path):
    run_probe(tmp_path / "xlsx", 450)
    run_probe(tmp_path / "zip", 450, "zip")
    standalone = workbook_rows(tmp_path / "xlsx/export.xlsx", 450)
    with zipfile.ZipFile(tmp_path / "zip/export.zip") as archive:
        names = archive.namelist()
        excel = [name for name in names if name.endswith(".xlsx")]
        assert len(excel) == 1
        packaged = workbook_rows(BytesIO(archive.read(excel[0])), 450)
        assert [row[:-1] for row in standalone] == [row[:-1] for row in packaged]
        assert len([name for name in names if name.endswith(".wav")]) == 450
        assert len([name for name in names if name.endswith(".csv")]) == 450
        database = tmp_path / "copied.sqlite3"
        database.write_bytes(archive.read("copy_data.sqlite3"))
        with sqlite3.connect(database) as conn:
            assert conn.execute("SELECT count(*) FROM selected_runs").fetchone()[0] == 450


@pytest.mark.parametrize("mode", ["cancel", "failure"])
def test_real_export_terminal_paths_preserve_target_and_clean_staging(tmp_path, mode):
    run_probe(tmp_path / mode, 5, mode)
