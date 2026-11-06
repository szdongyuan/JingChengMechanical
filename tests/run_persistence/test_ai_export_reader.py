from __future__ import annotations

from io import BytesIO
import json
from uuid import uuid4
from zipfile import ZipFile

from openpyxl import load_workbook
import peewee
import pytest

from app.service.run_persistence.ai_export_reader import RunAiExportReader
from app.service.run_persistence.analysis_result_artifact import AnalysisResultArtifactStore
from app.service.run_persistence.export_reader import RunExportReader
from app.service.run_persistence.prepare_database import prepare_run_persistence_database
from app.service.run_record_export.archive import RunArchiveService
from app.service.run_record_export.contracts import AiExportValue, RunDataExportOptions
from tests.run_persistence.test_export_reader import (
    _finish, _insert_active, _insert_attempt, _insert_audio, _ready_runtime,
)


@pytest.fixture
def persisted(tmp_path):
    runtime = _ready_runtime(prepare_run_persistence_database(tmp_path / "run_v2.db"))
    run_id = _insert_active(runtime)
    audio_id = _insert_audio(runtime, run_id, tmp_path / "unused.wav")
    attempt_id = _insert_attempt(runtime, run_id, audio_id)
    _finish(runtime, run_id)
    # Deliberately separate from the DB directory: diagnostics follow the store.
    store = AnalysisResultArtifactStore(tmp_path / "configured" / "run_results")
    reader = RunAiExportReader(runtime, result_artifact_store=store)
    return runtime, run_id, attempt_id, store, reader


def _save(persisted, result, *, compressed=False):
    runtime, _, attempt_id, store, _ = persisted
    # JSON exponent overflow passes SQLite's json_valid but is not a finite score.
    payload = json.dumps({"judgment": "PASS", "result": result}).replace(
        "Infinity", "1e999",
    )
    if compressed:
        published = store.publish(attempt_id, "PASS", payload)
        payload = published.reference_json
        store.settle_publication(published, retain=True)
    with runtime.operation() as database:
        database.execute_sql(
            "UPDATE analysis_attempt SET result_payload_json = ? WHERE attempt_id = ?",
            (payload, attempt_id),
        )
    return payload


def _response(persisted, text):
    _, _, attempt_id, store, _ = persisted
    path = store.root.parent / "ai_runs" / attempt_id / "response.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read(persisted):
    _, run_id, _, _, reader = persisted
    return reader.get_current_effective_export_values((run_id,))[run_id]


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("score,label", [(0, " PASS "), (0.875, " 自定义标签 ")])
def test_legacy_result_recovers_score_and_same_attempt_raw_label(
    persisted, compressed, score, label,
):
    _save(persisted, {"ai_score": score}, compressed=compressed)
    _response(persisted, json.dumps({"ai_result": {"ai_label": label, "score": 0.123}}))
    assert _read(persisted) == AiExportValue(score, label)


@pytest.mark.parametrize("response", [
    None, "{bad", "[]", '{"ai_result":null}',
    '{"ai_result":{"ai_label":false}}',
])
def test_unavailable_legacy_response_keeps_saved_score(persisted, response):
    _save(persisted, {"ai_score": 0})
    if response is not None:
        _response(persisted, response)
    assert _read(persisted) == AiExportValue(0, None)


@pytest.mark.parametrize("snapshot,expected", [
    ({"status": "success", "score": 0, "ai_label": " NG "}, AiExportValue(0, " NG ")),
    ({"status": "success"}, AiExportValue(None, None)),
    ({"status": "success", "ai_label": " raw "}, AiExportValue(None, " raw ")),
    ({"status": "success", "score": 0}, AiExportValue(0, None)),
    ({"status": "success", "score": True, "ai_label": " \t "}, AiExportValue(None, None)),
    ({"status": "success", "score": "0.7", "ai_label": 17}, AiExportValue(None, None)),
    ({"status": "success", "score": float("inf"), "ai_label": "label"}, AiExportValue(None, "label")),
    ({"status": "disabled", "score": 0.8, "ai_label": "stale"}, AiExportValue(None, None)),
    ({"status": "failed", "score": 0.8, "ai_label": "stale"}, AiExportValue(None, None)),
    ({"status": "skipped"}, AiExportValue(None, None)),
    ({"status": "process_error"}, AiExportValue(None, None)),
    ({"score": 0.8, "ai_label": "stale"}, AiExportValue(None, None)),
    (None, AiExportValue(None, None)),
])
def test_snapshot_precedence_and_value_validation(persisted, snapshot, expected):
    _save(persisted, {"ai_score": 0.99, "review": {"ai_result": snapshot}})
    _response(persisted, '{"ai_result":{"ai_label":"legacy conflict","score":0.99}}')
    assert _read(persisted) == expected


@pytest.mark.parametrize("result", [
    {}, {"ai_score": False}, {"ai_score": "0.3"}, {"ai_score": float("inf")},
])
def test_legacy_unavailable_score_is_blank(persisted, result):
    _save(persisted, result)
    assert _read(persisted) == AiExportValue(None, None)


def test_no_successful_attempt_returns_blank(persisted):
    runtime, _, _, _, reader = persisted
    run_id = _insert_active(runtime)
    assert reader.get_current_effective_export_values((run_id,)) == {
        run_id: AiExportValue(None, None),
    }


def test_latest_successful_reanalysis_ignores_later_failure_and_manual_fault(persisted):
    runtime, run_id, attempt_id, _, _ = persisted
    _save(persisted, {"ai_score": 0.1})
    _response(persisted, '{"ai_result":{"ai_label":"old label"}}')
    latest = str(uuid4())
    payload = json.dumps({"judgment": "PASS", "result": {"ai_score": 0.9}})
    with runtime.operation() as database:
        database.execute_sql(
            "INSERT INTO analysis_attempt SELECT ?, run_id, audio_id, 'REANALYSIS', "
            "'SUCCEEDED', judgment, ?, NULL, NULL, queued_at, started_at, "
            "'2026-07-28T12:35:56.123456Z', updated_at FROM analysis_attempt WHERE attempt_id = ?",
            (latest, payload, attempt_id),
        )
        database.execute_sql(
            "INSERT INTO analysis_attempt SELECT ?, run_id, audio_id, 'REANALYSIS', "
            "'FAILED', NULL, NULL, 'ERROR', 'failed', queued_at, started_at, "
            "'2026-07-29T12:35:56.123456Z', updated_at FROM analysis_attempt WHERE attempt_id = ?",
            (str(uuid4()), attempt_id),
        )
        database.execute_sql(
            "UPDATE run SET judgment='FAIL', judgment_source='MANUAL', "
            "fault_type='manual fault', manual_judgment_revision=1 WHERE run_id=?", (run_id,),
        )
    # Missing latest response must never borrow the older successful label.
    assert _read(persisted) == AiExportValue(0.9, None)
    latest_fixture = runtime, run_id, latest, persisted[3], persisted[4]
    _response(latest_fixture, '{"ai_result":{"ai_label":" 最新标签 "}}')
    assert _read(persisted) == AiExportValue(0.9, " 最新标签 ")


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_unavailable_artifact_still_exports_blank_fields(persisted, tmp_path, damage):
    _, _, _, store, _ = persisted
    reference = _save(
        persisted,
        {"review": {"ai_result": {
            "status": "success", "score": 0.5, "ai_label": "PASS",
        }}},
        compressed=True,
    )
    path = store.path_for_reference(reference)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt")
    assert _read(persisted) == AiExportValue(None, None)
    _assert_packages(persisted, tmp_path, None, None)


@pytest.mark.parametrize("payload", ["[]", '{"judgment":"PASS","result":null}'])
def test_invalid_inline_result_is_unavailable(persisted, payload):
    runtime, _, attempt_id, _, _ = persisted
    with runtime.operation() as database:
        database.execute_sql(
            "UPDATE analysis_attempt SET result_payload_json=? WHERE attempt_id=?",
            (payload, attempt_id),
        )
    assert _read(persisted) == AiExportValue(None, None)


def test_database_query_error_propagates(persisted):
    runtime, _, _, _, _ = persisted
    with runtime.operation() as database:
        database.execute_sql("ALTER TABLE analysis_attempt RENAME TO unavailable_attempt")
    with pytest.raises(peewee.OperationalError, match="analysis_attempt"):
        _read(persisted)


def _assert_packages(persisted, tmp_path, score, label):
    runtime, run_id, _, store, reader = persisted
    service = RunArchiveService(
        RunExportReader(runtime, result_artifact_store=store), reader,
    )
    excel = tmp_path / "records.xlsx"
    service.export(
        (run_id,), str(excel), options=RunDataExportOptions(True, False, False, False),
    )
    workbook = load_workbook(excel)
    try:
        assert (workbook.active["H2"].value, workbook.active["I2"].value) == (score, label)
    finally:
        workbook.close()
    target = tmp_path / "records.zip"
    service.export(
        (run_id,), str(target), options=RunDataExportOptions(True, False, False, True),
    )
    with ZipFile(target) as archive:
        root = json.loads(archive.read("export_manifest.json"))
        manifest = json.loads(archive.read(root["records"][0]["manifest"]))
        assert manifest["ai"] == {"score": score, "label": label}
        workbook = load_workbook(BytesIO(archive.read("检测记录.xlsx")))
        try:
            assert (workbook.active["H2"].value, workbook.active["I2"].value) == (score, label)
        finally:
            workbook.close()


@pytest.mark.parametrize("snapshot", [False, True])
def test_compressed_persisted_values_agree_in_excel_and_zip(
    persisted, tmp_path, snapshot,
):
    ai = {"status": "success", "score": 0, "ai_label": " 原始标签 "}
    result = {"review": {"ai_result": ai}} if snapshot else {"ai_score": 0}
    _save(persisted, result, compressed=True)
    if not snapshot:
        _response(persisted, json.dumps({"ai_result": ai}))
    _assert_packages(persisted, tmp_path, 0, " 原始标签 ")
