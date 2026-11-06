from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from app.service.run_persistence.database import RunPersistenceDatabase
from tests.run_persistence.test_schema import _run_values, _audio_values, _attempt_values


@pytest.fixture
def populated_v2(raw_schema_database, tmp_path, now):
    path = tmp_path / 'legacy-v2.sqlite3'
    ddl = raw_schema_database.execute_sql(
        "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type DESC"
    ).fetchall()
    connection = sqlite3.connect(path)
    for (sql,) in ddl:
        connection.execute(sql.replace("container_format IN ('WAV_PCM', 'WAV_IEEE_FLOAT')",
                                       "container_format = 'WAV_PCM'"))
    connection.execute('PRAGMA application_id = 1245924688')
    connection.execute('PRAGMA user_version = 2')
    run = _run_values(now, state='SUCCEEDED', judgment='FAIL', judgment_source='MANUAL',
                      manual_judgment_revision=2, fault_type='reviewed', judged_at=now,
                      terminal_at=now, terminal_code='ANALYSIS_SUCCEEDED')
    audio = _audio_values(run['run_id'], now)
    attempt = _attempt_values(run['run_id'], audio['audio_id'], now, state='SUCCEEDED',
                              judgment='PASS', started_at=now, terminal_at=now,
                              result_payload_json='{"artifact_path":"results/original.npz"}')
    for table, values in [('run', run), ('audio', audio), ('analysis_attempt', attempt)]:
        connection.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                           tuple(values.values()))
    connection.commit()
    connection.close()
    return path


def rows(path):
    with closing(sqlite3.connect(path)) as connection:
        return {table: connection.execute(f'SELECT * FROM {table}').fetchall()
                for table in ('run', 'audio', 'analysis_attempt')}


def test_known_v2_migrates_with_consistent_backup_and_all_links_preserved(populated_v2):
    # Keep committed records in WAL to prove backup uses SQLite, not a file copy.
    wal_writer = sqlite3.connect(populated_v2)
    wal_writer.execute('PRAGMA journal_mode = WAL')
    wal_writer.execute("UPDATE run SET fault_type = 'latest WAL label'")
    wal_writer.commit()
    before = rows(populated_v2)
    try:
        result = RunPersistenceDatabase(populated_v2).open_and_validate_startup()
        assert result.ok, result.reason
        with closing(sqlite3.connect(populated_v2)) as connection:
            assert connection.execute('PRAGMA user_version').fetchone()[0] == 3
            assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
            connection.execute("UPDATE audio SET container_format = 'WAV_IEEE_FLOAT'")
            connection.rollback()
        assert rows(populated_v2) == before
        backups = list(populated_v2.parent.glob('legacy-v2.sqlite3.v2-backup-*.sqlite3'))
        assert len(backups) == 1
        assert rows(backups[0]) == before
        with closing(sqlite3.connect(backups[0])) as backup:
            assert backup.execute('PRAGMA user_version').fetchone()[0] == 2
            assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert RunPersistenceDatabase(populated_v2).open_and_validate_startup().ok
        assert list(populated_v2.parent.glob('legacy-v2.sqlite3.v2-backup-*.sqlite3')) == backups
    finally:
        wal_writer.close()


@pytest.mark.parametrize('change', ['ALTER TABLE audio ADD COLUMN surprise TEXT',
                                  'PRAGMA user_version = 99'])
def test_unknown_schema_is_rejected_without_migration(populated_v2, change):
    with closing(sqlite3.connect(populated_v2)) as connection:
        connection.execute(change)
    before = populated_v2.read_bytes()
    result = RunPersistenceDatabase(populated_v2).open_and_validate_startup()
    assert not result.ok
    assert populated_v2.read_bytes() == before
    assert list(populated_v2.parent.glob('*.v2-backup-*.sqlite3')) == []


def test_failed_final_validation_rolls_back_schema_and_rows(populated_v2, monkeypatch):
    from app.service.run_persistence import audio_format_migration as migration

    before = rows(populated_v2)
    real_require_schema = migration._require_schema
    def fail_after_rebuild(database, version, **kwargs):
        if version == 3:
            raise ValueError('injected post-rebuild validation failure')
        return real_require_schema(database, version, **kwargs)
    monkeypatch.setattr(migration, '_require_schema', fail_after_rebuild)

    result = RunPersistenceDatabase(populated_v2).open_and_validate_startup()

    assert not result.ok
    assert 'post-rebuild validation failure' in result.reason
    assert rows(populated_v2) == before
    with closing(sqlite3.connect(populated_v2)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE audio SET container_format = 'WAV_IEEE_FLOAT'")
    backups = list(populated_v2.parent.glob('*.v2-backup-*.sqlite3'))
    assert len(backups) == 1
    assert rows(backups[0]) == before
