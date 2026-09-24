"""Disk-backed regression coverage for compatible torque schema upgrades."""
import sqlite3

import pytest
from peewee import IntegrityError, SqliteDatabase

import app.service.db_connection_service as svc_mod
from tests.test_db_connection_service import _create_runtime_tables


CONFIG_COLUMNS = {
    "plc_id": ("INTEGER", 1, None, 1),
    "enabled": ("INTEGER", 1, "0", 0),
    "match_type": ("TEXT", 1, "'model'", 0),
    "read_db_number": ("INTEGER", 0, None, 0),
    "read_byte_offset": ("INTEGER", 0, None, 0),
    "write_db_number": ("INTEGER", 0, None, 0),
    "write_byte_offset": ("INTEGER", 0, None, 0),
}
RULE_COLUMNS = {
    "id": ("INTEGER", 1, None, 1),
    "match_type": ("TEXT", 1, None, 0),
    "match_key": ("TEXT", 1, None, 0),
    "torque_value": ("REAL", 1, None, 0),
}


def columns(database, name):
    return {row[1]: tuple(row[2:]) for row in database.execute_sql(
        f'PRAGMA table_info("{name}")'
    ).fetchall()}


def schema(database):
    return database.execute_sql(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    database = SqliteDatabase(path)
    database.connect()
    _create_runtime_tables(database)
    database.execute_sql("INSERT INTO test_config_table (id) VALUES (17)")
    monkeypatch.setattr(svc_mod, "DB_PATH", str(path))
    monkeypatch.setattr(svc_mod, "db", database)
    yield database, path
    if not database.is_closed():
        database.close()


@pytest.mark.parametrize("manual_present", [True, False])
def test_upgrade_preserves_business_rows_and_backs_up_all_changes_once(legacy_db, manual_present):
    database, path = legacy_db
    if not manual_present:
        database.execute_sql('ALTER TABLE test_config_table DROP COLUMN manual_judgment_buttons_visible')
    before = schema(database)
    svc_mod.DbConnectionService().connect()

    assert columns(database, "plc_torque_config_table") == CONFIG_COLUMNS
    assert columns(database, "torque_rule_table") == RULE_COLUMNS
    assert database.execute_sql(
        "SELECT id, manual_judgment_buttons_visible FROM test_config_table"
    ).fetchall() == [(17, 0)]
    database.execute_sql("INSERT INTO plc_torque_config_table (plc_id) VALUES (7)")
    assert database.execute_sql("SELECT * FROM plc_torque_config_table").fetchone() == (
        7, 0, "model", None, None, None, None,
    )
    database.execute_sql("INSERT INTO torque_rule_table (match_type, match_key, torque_value) VALUES ('SN', '0001', 2.5)")
    with pytest.raises(IntegrityError):
        database.execute_sql("INSERT INTO torque_rule_table (match_type, match_key, torque_value) VALUES ('SN', '0001', 3)")
    database.execute_sql("INSERT INTO torque_rule_table (match_type, match_key, torque_value) VALUES ('model', '0001', 3)")
    backups = list(path.parent.glob("legacy.db.backup-auto-*"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall() == before
        assert backup.execute("SELECT id FROM test_config_table").fetchall() == [(17,)]
    migrated = schema(database)
    database.close()
    svc_mod.DbConnectionService().connect()
    assert schema(database) == migrated
    assert list(path.parent.glob("legacy.db.backup-auto-*")) == backups
    assert database.execute_sql("SELECT match_key, torque_value FROM torque_rule_table ORDER BY id").fetchall() == [("0001", 2.5), ("0001", 3.0)]


def test_backup_failure_does_not_change_schema(legacy_db, monkeypatch):
    database, path = legacy_db
    before = schema(database)
    real_connect = sqlite3.connect

    def fail_backup(target, *args, **kwargs):
        if ".backup-auto-" in str(target):
            raise OSError("backup destination unavailable")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(svc_mod.sqlite3, "connect", fail_backup)
    with pytest.raises(svc_mod.DatabaseStartupError, match="backup failed"):
        svc_mod.DbConnectionService().connect()
    assert database.is_closed()
    assert schema(database) == before
    assert not list(path.parent.glob("legacy.db.backup-auto-*"))


def test_ddl_failure_rolls_back_manual_column_and_first_torque_table(legacy_db, monkeypatch):
    database, path = legacy_db
    database.execute_sql('ALTER TABLE test_config_table DROP COLUMN manual_judgment_buttons_visible')
    before = schema(database)
    execute = database.execute_sql
    reached = []

    def fail_second_table(sql, *args, **kwargs):
        if sql.startswith("CREATE TABLE") and '"torque_rule_table"' in sql:
            reached.append(True)
            assert "plc_id" in columns(database, "plc_torque_config_table")
            raise RuntimeError("injected DDL failure")
        return execute(sql, *args, **kwargs)

    monkeypatch.setattr(database, "execute_sql", fail_second_table)
    with pytest.raises(svc_mod.DatabaseStartupError, match="upgrade failed"):
        svc_mod.DbConnectionService().connect()
    assert reached == [True]
    assert database.is_closed()
    assert schema(database) == before
    assert len(list(path.parent.glob("legacy.db.backup-auto-*"))) == 1


@pytest.mark.parametrize("table, replacement", [
    ("plc_torque_config_table", "plc_id INTEGER PRIMARY KEY"),
    ("torque_rule_table", "id INTEGER PRIMARY KEY, match_type TEXT, match_key TEXT, torque_value TEXT"),
])
def test_malformed_existing_torque_table_is_rejected_without_mutation(legacy_db, table, replacement):
    database, path = legacy_db
    database.execute_sql(f'CREATE TABLE "{table}" ({replacement})')
    before = schema(database)
    with pytest.raises(svc_mod.DatabaseStartupError, match=table):
        svc_mod.DbConnectionService().connect()
    assert schema(database) == before
    assert not list(path.parent.glob("legacy.db.backup-auto-*"))


def test_invalid_existing_manual_column_is_rejected_before_torque_upgrade(legacy_db):
    database, path = legacy_db
    database.execute_sql('ALTER TABLE test_config_table DROP COLUMN manual_judgment_buttons_visible')
    database.execute_sql('ALTER TABLE test_config_table ADD COLUMN manual_judgment_buttons_visible TEXT')
    before = schema(database)
    with pytest.raises(svc_mod.DatabaseStartupError, match="manual_judgment_buttons_visible"):
        svc_mod.DbConnectionService().connect()
    assert schema(database) == before
    assert not list(path.parent.glob("legacy.db.backup-auto-*"))


def test_orm_tables_match_the_migration_contract(setup_test_db):
    from app.service.torque.migration import validate_torque_schema
    from model.plc_torque_config_table import PlcTorqueConfigTable
    from model.torque_rule_table import TorqueRuleTable

    assert columns(setup_test_db, "plc_torque_config_table") == CONFIG_COLUMNS
    assert columns(setup_test_db, "torque_rule_table") == RULE_COLUMNS
    validate_torque_schema(setup_test_db)
    config = PlcTorqueConfigTable.create(plc_id=7)
    assert (config.enabled, config.match_type, config.read_db_number) == (False, "model", None)
    rule = TorqueRuleTable.create(match_type="SN", match_key="000123", torque_value=2.5)
    assert TorqueRuleTable.get_by_id(rule.id).torque_value == 2.5


@pytest.mark.parametrize("index_sql", [
    None,
    "CREATE INDEX wrong ON torque_rule_table (match_type, match_key)",
    "CREATE UNIQUE INDEX wrong ON torque_rule_table (match_key, match_type)",
    "CREATE UNIQUE INDEX wrong ON torque_rule_table (match_type, match_key) WHERE torque_value > 0",
    "CREATE UNIQUE INDEX wrong ON torque_rule_table (match_type, match_key COLLATE NOCASE)",
])
def test_existing_invalid_rule_index_is_rejected_without_repair(legacy_db, index_sql):
    from app.service.torque.migration import apply_torque_migration

    database, path = legacy_db
    apply_torque_migration(database)
    database.execute_sql('DROP INDEX "torque_rule_table_match_type_match_key"')
    if index_sql:
        database.execute_sql(index_sql)
    before = schema(database)
    with pytest.raises(svc_mod.DatabaseStartupError, match="torque_rule_table unique index"):
        svc_mod.DbConnectionService().connect()
    assert schema(database) == before
    assert not list(path.parent.glob("legacy.db.backup-auto-*"))


@pytest.mark.parametrize("existing_table", ["plc_torque_config_table", "torque_rule_table"])
def test_one_existing_valid_torque_table_is_preserved(legacy_db, existing_table):
    from app.service.torque.migration import apply_torque_migration

    database, path = legacy_db
    apply_torque_migration(database)
    other_table = ({"plc_torque_config_table", "torque_rule_table"} - {existing_table}).pop()
    database.execute_sql(f'DROP TABLE "{other_table}"')
    if existing_table == "plc_torque_config_table":
        database.execute_sql("INSERT INTO plc_torque_config_table (plc_id) VALUES (7)")
    else:
        database.execute_sql("INSERT INTO torque_rule_table (match_type, match_key, torque_value) VALUES ('SN', '000123', 2.5)")
    rows = database.execute_sql(f'SELECT * FROM "{existing_table}"').fetchall()
    definition = columns(database, existing_table)
    svc_mod.DbConnectionService().connect()
    assert columns(database, "plc_torque_config_table") == CONFIG_COLUMNS
    assert columns(database, "torque_rule_table") == RULE_COLUMNS
    assert columns(database, existing_table) == definition
    assert database.execute_sql(f'SELECT * FROM "{existing_table}"').fetchall() == rows
    assert len(list(path.parent.glob("legacy.db.backup-auto-*"))) == 1


@pytest.mark.parametrize("field, definition", [
    ("enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("enabled", "INTEGER DEFAULT 0"),
    ("match_type", "TEXT NOT NULL DEFAULT 'SN'"),
    ("read_db_number", "TEXT"),
    ("read_byte_offset", "INTEGER NOT NULL"),
    ("plc_id", "INTEGER NOT NULL"),
])
def test_existing_config_requires_exact_column_contract(legacy_db, field, definition):
    database, path = legacy_db
    declarations = {
        "plc_id": "INTEGER NOT NULL PRIMARY KEY",
        "enabled": "INTEGER NOT NULL DEFAULT 0",
        "match_type": "TEXT NOT NULL DEFAULT 'model'",
        "read_db_number": "INTEGER",
        "read_byte_offset": "INTEGER",
        "write_db_number": "INTEGER",
        "write_byte_offset": "INTEGER",
    }
    declarations[field] = definition
    fields = ", ".join(f'"{name}" {sql}' for name, sql in declarations.items())
    database.execute_sql(f'CREATE TABLE "plc_torque_config_table" ({fields})')
    before = schema(database)
    with pytest.raises(svc_mod.DatabaseStartupError, match="plc_torque_config_table column definitions"):
        svc_mod.DbConnectionService().connect()
    assert schema(database) == before
    assert not list(path.parent.glob("legacy.db.backup-auto-*"))
