import threading

import pytest
from peewee import IntegrityError, SqliteDatabase

from app.service.torque.migration import apply_torque_migration
from app.service.torque.store import TorquePreviewConflict, TorqueStore
from app.service.torque.types import TorqueConfig, TorqueImportRow
from app.service.torque.validation import validate_torque


@pytest.fixture
def database(tmp_path):
    db = SqliteDatabase(tmp_path / 'torque.db')
    db.connect()
    apply_torque_migration(db)
    db.execute_sql('CREATE TABLE plc_table (id INTEGER PRIMARY KEY)')
    db.execute_sql('INSERT INTO plc_table VALUES (7)')
    yield db
    db.close()


@pytest.fixture
def store(database):
    return TorqueStore(database)


def test_config_defaults_save_reload_and_delete_preserves_rules(store, database):
    assert store.get_config(7) == TorqueConfig(7)
    saved = store.save_config(TorqueConfig(7, True, 'sn', None, '20.0', 2, 24), 2)
    assert saved.read_byte_offset == 20
    assert saved.revision == 1
    assert store.snapshot().revision == 0
    store.upsert_rule('sn', ' 000123 ', '2.6')
    reloaded = TorqueStore(database)
    assert reloaded.get_config(7).enabled
    assert reloaded.snapshot().values[('sn', '000123')] == validate_torque('2.6')
    store.delete_config(7)
    assert not store.get_config(7).enabled
    assert ('sn', '000123') in store.snapshot().values
    assert not TorqueStore(database).get_config(7).enabled


def test_config_validation_and_deleted_plc_rejection(store, database):
    for config in (TorqueConfig(7, True), TorqueConfig(7, True, 'model', None, 20, 2, 22), TorqueConfig(7, False, read_byte_offset='1.2')):
        with pytest.raises(ValueError):
            store.save_config(config, 2)
    database.execute_sql('DELETE FROM plc_table WHERE id = 7')
    with pytest.raises(ValueError, match='PLC'):
        store.save_config(TorqueConfig(7), 2)
    assert database.execute_sql('SELECT count(*) FROM plc_torque_config_table').fetchone()[0] == 0


def test_crud_unique_exact_keys_and_immutable_versions(store):
    original = store.snapshot()
    store.create_rule('model', ' M100 ', 2.5)
    with pytest.raises(ValueError):
        store.create_rule('model', 'M100', 3)
    store.create_rule('sn', 'M100', -2)
    store.create_rule('model', 'm100', 0)
    store.update_rule('model', 'M100', 'sn', '000123', 2.6)
    with pytest.raises(ValueError):
        store.update_rule('sn', '000123', 'sn', 'M100', 4)
    assert ('model', 'M100') not in store.snapshot().values
    store.delete_rule('model', ' m100 ')
    assert set(store.snapshot().values) == {('sn', 'M100'), ('sn', '000123')}
    assert not original.values
    with pytest.raises(TypeError):
        store.snapshot().values[('sn', 'new')] = 1
    before = store.snapshot()
    store.upsert_rule('sn', 'M100', -2)
    store.delete_rule('sn', 'missing')
    assert store.snapshot() is before


def test_preview_overwrites_additions_preserves_absent_rows(store, database):
    store.upsert_rule('model', 'keep', 1)
    store.upsert_rule('model', 'M100', 2)
    before = store.snapshot()
    preview = store.preview_import([TorqueImportRow(2, 'model', ' M100 ', 2.5), TorqueImportRow(3, 'sn', '000123', 0)])
    assert preview.revision == before.revision
    assert preview.additions == (TorqueImportRow(3, 'sn', '000123', 0),)
    assert preview.overwrites[0].old_value == 2
    assert preview.overwrites[0].row.torque_value == 2.5
    assert store.snapshot() is before
    store.commit_import(preview)
    assert dict(TorqueStore(database).snapshot().values) == {('model', 'keep'): 1, ('model', 'M100'): 2.5, ('sn', '000123'): 0}


def test_preview_cannot_overwrite_unreviewed_changes(store):
    preview = store.preview_import([TorqueImportRow(2, 'model', 'M100', 2.5)])
    store.upsert_rule('model', 'M100', 3.0)
    with pytest.raises(TorquePreviewConflict):
        store.commit_import(preview)
    assert store.snapshot().values[('model', 'M100')] == 3.0


def test_invalid_or_duplicate_rows_block_entire_import(store):
    preview = store.preview_import([TorqueImportRow(2, 'sn', '000123', 2), TorqueImportRow(3, 'sn', ' 000123 ', 3), TorqueImportRow(4, 'model', 'bad', True)])
    assert {error.row_number for error in preview.errors} == {3, 4}
    with pytest.raises(ValueError):
        store.commit_import(preview)
    assert not store.snapshot().values


def test_sqlite_mid_import_failure_rolls_back_database_and_snapshot(store, database):
    store.upsert_rule('model', 'M100', 1)
    before = store.snapshot()
    database.execute_sql("CREATE TRIGGER reject_bad BEFORE INSERT ON torque_rule_table WHEN NEW.match_key = 'bad' BEGIN SELECT RAISE(ABORT, 'rejected'); END")
    preview = store.preview_import([TorqueImportRow(2, 'model', 'M100', 2), TorqueImportRow(3, 'model', 'bad', 3)])
    with pytest.raises(IntegrityError):
        store.commit_import(preview)
    assert store.snapshot() is before
    assert dict(TorqueStore(database).snapshot().values) == dict(before.values)


def test_prepare_snapshot_failure_keeps_database_unchanged(store, database, monkeypatch):
    store.upsert_rule('model', 'M100', 1)
    before = store.snapshot()
    preview = store.preview_import([TorqueImportRow(2, 'model', 'M100', 2)])
    def fail(*args):
        raise RuntimeError('allocation failed')
    monkeypatch.setattr('app.service.torque.store.RuleSnapshot', fail)
    with pytest.raises(RuntimeError):
        store.commit_import(preview)
    assert store.snapshot() is before
    assert database.execute_sql('SELECT torque_value FROM torque_rule_table').fetchone()[0] == 1


def test_import_thread_uses_own_connection_without_blocking_snapshot(store, database):
    started, release, read_done = threading.Event(), threading.Event(), threading.Event()
    errors = []
    before = store.snapshot()
    def pause(value):
        started.set()
        if not release.wait(5):
            raise RuntimeError('test timed out')
        return value
    database.register_function(pause, 'pause_write', 1)
    database.execute_sql('CREATE TRIGGER pause_insert BEFORE INSERT ON torque_rule_table BEGIN SELECT pause_write(NEW.torque_value); END')
    def write():
        try:
            with database.connection_context():
                store.commit_import(store.preview_import([TorqueImportRow(2, 'sn', '000123', 2)]))
        except Exception as error:
            errors.append(error)
    observed = []
    def read():
        observed.append(store.snapshot())
        read_done.set()
    worker = threading.Thread(target=write)
    reader = threading.Thread(target=read)
    worker.start()
    try:
        assert started.wait(3)
        reader.start()
        assert read_done.wait(1), 'snapshot read waited for import transaction'
        assert observed == [before]
    finally:
        release.set()
        worker.join(5)
        if reader.ident:
            reader.join(5)
    assert not errors
    assert not worker.is_alive()
    assert store.snapshot().revision == before.revision + 1


def test_actual_sqlite_commit_failure_rolls_back_both_states(store, database):
    store.upsert_rule('model', 'M100', 1)
    before = store.snapshot()
    database.execute_sql('PRAGMA foreign_keys = ON')
    database.execute_sql('CREATE TABLE required_parent (id INTEGER PRIMARY KEY)')
    database.execute_sql('CREATE TABLE deferred_check (parent_id INTEGER REFERENCES required_parent(id) DEFERRABLE INITIALLY DEFERRED)')
    database.execute_sql('CREATE TRIGGER fail_at_commit AFTER UPDATE ON torque_rule_table BEGIN INSERT INTO deferred_check VALUES (999); END')
    with pytest.raises(IntegrityError):
        store.upsert_rule('model', 'M100', 2)
    assert store.snapshot() is before
    assert database.execute_sql('SELECT torque_value FROM torque_rule_table').fetchone()[0] == 1
    assert database.execute_sql('SELECT count(*) FROM deferred_check').fetchone()[0] == 0


def test_config_failure_keeps_previous_memory_and_database_config(store, database):
    old = store.save_config(TorqueConfig(7), 2)
    database.execute_sql("CREATE TRIGGER fail_config BEFORE UPDATE ON plc_torque_config_table BEGIN SELECT RAISE(ABORT, 'rejected'); END")
    with pytest.raises(IntegrityError):
        store.save_config(TorqueConfig(7, True, 'model', 2, 20, 2, 24), 2)
    assert store.get_config(7) is old
    assert not TorqueStore(database).get_config(7).enabled


def test_nested_transaction_cannot_publish_uncommitted_changes(store, database):
    before = store.snapshot()
    with database.atomic():
        with pytest.raises(RuntimeError, match='own their database transaction'):
            store.upsert_rule('model', 'M100', 2)
    assert store.snapshot() is before
    assert not TorqueStore(database).snapshot().values


@pytest.mark.parametrize('kind,key,value', [('SN', 'x', 2), ('sn', 123, 2), ('sn', '', 2), ('sn', 'x', True), ('sn', 'x', '1e-1000')])
def test_crud_uses_shared_validation(store, kind, key, value):
    with pytest.raises(ValueError):
        store.upsert_rule(kind, key, value)
    assert not store.snapshot().values


def test_parsed_errors_block_even_valid_rows(store, tmp_path):
    from openpyxl import Workbook
    from app.service.torque.excel import parse_workbook
    book = Workbook()
    book.active.append(['匹配类型', '型号或SN', '扭矩'])
    book.active.append(['型号', 'M100', 2])
    book.active.append(['SN', 123, 3])
    path = tmp_path / 'mixed.xlsx'
    book.save(path)
    book.close()
    preview = store.preview_import(parse_workbook(path))
    assert len(preview.additions) == 1
    assert preview.errors[0].row_number == 3
    with pytest.raises(ValueError):
        store.commit_import(preview)
    assert not store.snapshot().values


def test_saved_values_restore_after_connection_reopen(store, database):
    expected = store.upsert_rule('sn', '000123', '2.6').values[('sn', '000123')]
    store.save_config(TorqueConfig(7, True, 'sn', 2, 20, 2, 24), 2)
    database.close()
    reopened = SqliteDatabase(database.database)
    with reopened.connection_context():
        restored = TorqueStore(reopened)
        assert restored.snapshot().values[('sn', '000123')] == expected
        assert restored.get_config(7).write_byte_offset == 24


def test_coordinated_plc_delete_rolls_back_all_rows_and_publishes_after_commit(store, database):
    database.execute_sql('CREATE TABLE plc_field_offset_table (plc_id INTEGER PRIMARY KEY)')
    database.execute_sql('INSERT INTO plc_field_offset_table VALUES (7)')
    old = store.save_config(TorqueConfig(7, True, "model", 1, 20, 1, 24), 1)
    rules = store.upsert_rule('sn', 'keep', 2)
    database.execute_sql("CREATE TRIGGER reject_delete BEFORE DELETE ON plc_table BEGIN SELECT RAISE(ABORT, 'reject'); END")
    with pytest.raises(IntegrityError):
        store.delete_plc(7)
    assert store.get_config(7) is old
    for table in ('plc_table', 'plc_field_offset_table', 'plc_torque_config_table'):
        assert database.execute_sql(f'SELECT count(*) FROM {table}').fetchone()[0] == 1
    database.execute_sql('DROP TRIGGER reject_delete')
    deleted = store.delete_plc(7)
    assert not deleted.enabled and deleted.revision == old.revision + 1
    assert store.get_config(7) is deleted
    assert store.snapshot() is rules
    for table in ('plc_table', 'plc_field_offset_table', 'plc_torque_config_table'):
        assert database.execute_sql(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
