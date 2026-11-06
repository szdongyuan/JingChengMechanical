"""Compatible torque DDL on the explicitly supplied, already validated database."""


class TorqueSchemaError(ValueError):
    """An existing torque table cannot be safely upgraded automatically."""


_COLUMNS = {
    "plc_torque_config_table": {
        "plc_id": ("INTEGER", 1, None, 1),
        "enabled": ("INTEGER", 1, "0", 0),
        "match_type": ("TEXT", 1, "'model'", 0),
        "read_db_number": ("INTEGER", 0, None, 0),
        "read_byte_offset": ("INTEGER", 0, None, 0),
        "write_db_number": ("INTEGER", 0, None, 0),
        "write_byte_offset": ("INTEGER", 0, None, 0),
    },
    "torque_rule_table": {
        "id": ("INTEGER", 1, None, 1),
        "match_type": ("TEXT", 1, None, 0),
        "match_key": ("TEXT", 1, None, 0),
        "torque_value": ("REAL", 1, None, 0),
    },
}


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _existing_tables(db):
    return set(db.get_tables())


def _validate_table(db, table):
    actual = {
        row[1]: (row[2].upper(), row[3], row[4], row[5])
        for row in db.execute_sql(f"PRAGMA table_info({_quote(table)})").fetchall()
    }
    if actual != _COLUMNS[table]:
        raise TorqueSchemaError(f"Database schema is incomplete: invalid {table} column definitions.")
    if table == "torque_rule_table":
        indexes = db.execute_sql('PRAGMA index_list("torque_rule_table")').fetchall()
        for _, name, unique, _, partial in indexes:
            if not unique or partial:
                continue
            keys = [row for row in db.execute_sql(
                f"PRAGMA index_xinfo({_quote(name)})"
            ).fetchall() if row[5]]
            if [(row[2], row[3], row[4]) for row in keys] == [
                ("match_type", 0, "BINARY"), ("match_key", 0, "BINARY"),
            ]:
                return
        raise TorqueSchemaError("Database schema is incomplete: invalid torque_rule_table unique index.")


def needs_torque_migration(db):
    """Reject malformed existing tables before backup; only missing tables are pending."""
    existing = _existing_tables(db)
    for table in _COLUMNS.keys() & existing:
        _validate_table(db, table)
    return not _COLUMNS.keys() <= existing


def apply_torque_migration(db):
    """Create missing tables inside the caller's backed-up transaction."""
    needs_torque_migration(db)
    existing = _existing_tables(db)
    for table, fields in _COLUMNS.items():
        if table in existing:
            continue
        definitions = []
        for name, (kind, required, default, primary_key) in fields.items():
            definition = f"{_quote(name)} {kind}"
            if required:
                definition += " NOT NULL"
            if primary_key:
                definition += " PRIMARY KEY"
            if default is not None:
                definition += f" DEFAULT {default}"
            definitions.append(definition)
        db.execute_sql(f"CREATE TABLE {_quote(table)} ({', '.join(definitions)})")
        if table == "torque_rule_table":
            db.execute_sql(
                'CREATE UNIQUE INDEX "torque_rule_table_match_type_match_key" '
                'ON "torque_rule_table" ("match_type", "match_key")'
            )


def validate_torque_schema(db):
    for table in _COLUMNS:
        _validate_table(db, table)
