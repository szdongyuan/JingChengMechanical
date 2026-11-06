from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4
import wave

import pytest

from app.service.run_persistence.analysis_result_artifact import AnalysisResultArtifactStore
from app.service.run_persistence.data_management import RunDataManagementService
from app.service.run_persistence.historical_analysis_reader import (
    HistoricalAnalysisReadError,
    HistoricalAnalysisReader,
)
from tests.run_persistence.test_export_reader import (
    ACCEPTED_AT,
    TERMINAL_AT,
    _finish,
    _insert_active,
    _insert_audio,
    _ready_runtime,
)


@pytest.fixture
def saved_run(prepared_database_path, tmp_path):
    runtime = _ready_runtime(prepared_database_path)
    run_id = _insert_active(
        runtime, channel_order=(3, 1), fault_types=("轴承异响",),
        workorder={"sn": "SN-history", "model": "M-history"},
    )
    _finish(runtime, run_id)
    path = tmp_path / "saved.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00\x01\x00" * 16)
    audio_id = _insert_audio(runtime, run_id, path, channel_count=2)
    with runtime.operation() as database:
        database.execute_sql("UPDATE audio SET byte_size = ? WHERE run_id = ?",
                             (path.stat().st_size, run_id))
    store = AnalysisResultArtifactStore(tmp_path / "results")
    review = {
        "summary_payload": {"rows": [{"channel": 3, "spl": 71.25,
                                        "loudness": 4.2, "ai_result": "PASS"}]},
        "chart_payload": {"3": {"spectrum": {"x": [100, 200], "y": [1, 2]}}},
    }
    yield runtime, run_id, audio_id, path, store, review
    runtime.close_after_upstream_stopped()


def _save_attempt(saved_run, *, review=None, compressed=True,
                  terminal_at=TERMINAL_AT, state="SUCCEEDED"):
    runtime, run_id, audio_id, _path, store, default_review = saved_run
    attempt_id = str(uuid4())
    payload = json.dumps({"judgment": "PASS", "result": default_review if review is None else review})
    artifact_path = None
    if compressed and state == "SUCCEEDED":
        published = store.publish(attempt_id, "PASS", payload)
        payload = published.reference_json
        artifact_path = published.path
        store.settle_publication(published, retain=True)
    with runtime.operation() as database:
        database.execute_sql(
            """INSERT INTO analysis_attempt (
                attempt_id, run_id, audio_id, attempt_kind, state, judgment,
                result_payload_json, error_code, queued_at, started_at,
                terminal_at, updated_at
            ) VALUES (?, ?, ?, 'REANALYSIS', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (attempt_id, run_id, audio_id, state,
             "PASS" if state == "SUCCEEDED" else None,
             payload if state == "SUCCEEDED" else None,
             None if state == "SUCCEEDED" else "FAILED",
             ACCEPTED_AT, ACCEPTED_AT, terminal_at, terminal_at),
        )
    return attempt_id, artifact_path


def _reader(saved_run):
    return HistoricalAnalysisReader(saved_run[0], result_artifact_store=saved_run[4])


@pytest.mark.parametrize("compressed", [False, True])
def test_reads_frozen_metadata_and_latest_success_despite_later_failure(saved_run, compressed):
    _save_attempt(saved_run, review={"summary_payload": {}, "chart_payload": {}},
                  terminal_at="2026-07-27T12:35:00.123456Z")
    attempt_id, _ = _save_attempt(saved_run, compressed=compressed)
    _save_attempt(saved_run, state="FAILED", terminal_at="2026-07-27T12:36:00.123456Z")

    snapshot = _reader(saved_run).read(saved_run[1])

    assert snapshot.run.run_id == saved_run[1]
    assert snapshot.run.accepted_at == ACCEPTED_AT
    assert snapshot.config == {"config_id": 7, "label": "配置", "fault_types": ["轴承异响"]}
    assert snapshot.workorder == {"sn": "SN-history", "model": "M-history"}
    assert snapshot.successful_attempt_id == attempt_id
    assert snapshot.original_analysis_judgment == "PASS"
    assert snapshot.review == saved_run[5]
    assert snapshot.accepted_fault_types == ("轴承异响",)
    assert snapshot.audio.run_id == saved_run[1]
    assert snapshot.audio.source_wav_path == saved_run[3]
    assert snapshot.audio.channel_order == (3, 1)
    assert snapshot.audio.sample_rate_hz == 8000
    assert snapshot.audio.channel_count == 2
    assert snapshot.audio.frame_count == 16
    assert snapshot.result_message is None
    assert snapshot.charts_message is None
    assert snapshot.audio_message is None


def test_manual_label_preserves_original_payload_config_and_audio_bytes(saved_run):
    _attempt_id, artifact_path = _save_attempt(saved_run)
    before = _reader(saved_run).read(saved_run[1])
    artifact_bytes = artifact_path.read_bytes()
    audio_bytes = saved_run[3].read_bytes()

    assert RunDataManagementService(saved_run[0]).update_fault_type(saved_run[1], "轴承异响")
    after = _reader(saved_run).read(saved_run[1])

    assert after.run.judgment == "FAIL"
    assert after.run.judgment_source == "MANUAL"
    assert after.run.fault_type == "轴承异响"
    assert after.original_analysis_judgment == before.original_analysis_judgment == "PASS"
    assert after.review == before.review == saved_run[5]
    assert after.config == before.config
    assert after.run.config_snapshot_json == before.run.config_snapshot_json
    assert artifact_path.read_bytes() == artifact_bytes
    assert saved_run[3].read_bytes() == audio_bytes


def test_missing_audio_file_keeps_saved_result_and_labels(saved_run):
    _save_attempt(saved_run)
    saved_run[3].unlink()

    snapshot = _reader(saved_run).read(saved_run[1])

    assert snapshot.review == saved_run[5]
    assert snapshot.audio is None
    assert "不可用" in snapshot.audio_message
    assert snapshot.accepted_fault_types == ("轴承异响",)


def test_run_without_audio_fact_or_attempt_is_still_readable(saved_run):
    with saved_run[0].operation() as database:
        database.execute_sql("DELETE FROM audio WHERE run_id = ?", (saved_run[1],))

    snapshot = _reader(saved_run).read(saved_run[1])

    assert snapshot.run.run_id == saved_run[1]
    assert snapshot.audio is None
    assert snapshot.review is None
    assert snapshot.successful_attempt_id is None
    assert "未保存" in snapshot.audio_message
    assert "未保存" in snapshot.result_message


@pytest.mark.parametrize("damage", ["missing", "corrupt", "invalid_payload", "no_attempt"])
def test_unavailable_results_keep_metadata_and_audio(saved_run, damage):
    if damage != "no_attempt":
        _attempt_id, path = _save_attempt(saved_run, review=[] if damage == "invalid_payload" else None)
        if damage == "missing":
            path.unlink()
        elif damage == "corrupt":
            path.write_bytes(b"corrupt result")

    snapshot = _reader(saved_run).read(saved_run[1])

    assert snapshot.run.run_id == saved_run[1]
    assert snapshot.workorder["sn"] == "SN-history"
    assert snapshot.audio.source_wav_path == saved_run[3]
    assert snapshot.review is None
    assert snapshot.result_message
    assert snapshot.accepted_fault_types == ("轴承异响",)


def test_lightweight_decision_keeps_metrics_with_explicit_missing_charts(saved_run):
    decision = {"summary_payload": saved_run[5]["summary_payload"], "chart_payload": {}}
    _save_attempt(saved_run, review=decision)

    snapshot = _reader(saved_run).read(saved_run[1])

    assert snapshot.review == decision
    assert snapshot.result_message is None
    assert "尚无已保存图表" in snapshot.charts_message


@pytest.mark.parametrize("run_id", [str(uuid4()), "not-a-uuid"])
def test_missing_or_invalid_run_has_clear_primary_error(saved_run, run_id):
    with pytest.raises(HistoricalAnalysisReadError, match="记录"):
        _reader(saved_run).read(run_id)


def test_invalid_frozen_envelope_has_clear_primary_error(saved_run):
    with saved_run[0].operation() as database:
        database.execute_sql("UPDATE run SET config_snapshot_json = '{}' WHERE run_id = ?", (saved_run[1],))
    with pytest.raises(HistoricalAnalysisReadError, match="配置"):
        _reader(saved_run).read(saved_run[1])


def test_container_wires_reader_to_shared_runtime_and_artifact_store(saved_run, monkeypatch):
    from unittest.mock import MagicMock
    import app.services_container as container_module
    from tests.test_service_container import _PreparedOnlyBuilder

    monkeypatch.setattr(container_module.DbConnectionService, "connect", lambda self: None)
    for name in ("HardwareSelectService", "InputCalibrationService", "ConfigService", "AiService"):
        monkeypatch.setattr(container_module, name, MagicMock(return_value=MagicMock()))
    services = container_module.ServiceContainer(
        run_persistence_database=saved_run[0],
        production_composition_builder=_PreparedOnlyBuilder(),
    )

    reader = services.historical_analysis_reader
    assert type(reader) is HistoricalAnalysisReader
    assert reader._runtime is saved_run[0]
    assert reader._result_artifact_store is services.analysis_result_artifact_store
    assert reader._result_artifact_store is services.run_export_reader._result_artifact_store
    assert reader.read(saved_run[1]).workorder["sn"] == "SN-history"


def test_replay_only_read_never_hydrates_results(saved_run, monkeypatch):
    reader = _reader(saved_run)
    def forbidden(*args):
        pytest.fail('replay metadata must not hydrate charts')
    monkeypatch.setattr(reader, '_read_result', forbidden)
    snapshot = reader.read_replay(saved_run[1])
    assert snapshot.run_id == saved_run[1]
    assert snapshot.audio.channel_order == (3, 1)
    assert snapshot.config['config_id'] == 7
    assert snapshot.audio_message is None
    saved_run[3].unlink()
    assert reader.read_replay(saved_run[1]).audio is None
    with pytest.raises(HistoricalAnalysisReadError):
        reader.read_replay('invalid')


def test_replay_only_import_uses_original_wav_columns(prepared_database_path, tmp_path):
    from app.service.run_persistence.contracts import IngressKind
    from tests.run_persistence.test_export_reader import _analysis_envelope
    runtime = _ready_runtime(prepared_database_path)
    try:
        run = _insert_active(runtime, ingress_kind=IngressKind.IMPORTED, channel_order=(7, 3))
        envelope = _analysis_envelope()
        envelope['config']['selected_channels'] = [7]
        envelope['channel_mapping'] = {
            'schema': 'audio-import-map/v2', 'source_channel_count': 2,
            'assignments': [
                {'wav_channel_index': 1, 'mode': 'config', 'channel_index': 7},
                {'wav_channel_index': 0, 'mode': 'chart', 'channel_index': None},
            ],
        }
        path = tmp_path / 'imported.wav'
        with wave.open(str(path), 'wb') as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b'\x00\x00\x01\x00' * 16)
        _insert_audio(runtime, run, path, channel_count=2, source_kind='IMPORTED_SOURCE')
        with runtime.operation() as database:
            database.execute_sql('UPDATE run SET config_snapshot_json=? WHERE run_id=?', (json.dumps(envelope), run))
            database.execute_sql('UPDATE audio SET byte_size=? WHERE run_id=?', (path.stat().st_size, run))
        reader = HistoricalAnalysisReader(runtime)
        snapshot = reader.read_replay(run)
        assert snapshot.audio.channel_order == (0, 1)
        assert snapshot.audio.frame_count / snapshot.audio.sample_rate_hz == 0.002
        assert snapshot.config['selected_channels'] == [7]
        with runtime.operation() as database:
            database.execute_sql('UPDATE audio SET sample_rate_hz=16000 WHERE run_id=?', (run,))
        assert reader.read_replay(run).audio is None
    finally:
        runtime.close_after_upstream_stopped()
