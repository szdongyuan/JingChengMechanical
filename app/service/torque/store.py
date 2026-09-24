"""Atomic persistence with immutable, inexpensive PLC-side snapshot reads.

The caller owns the explicitly supplied Peewee SQLite database and its connection
lifetime. Worker threads may use database.connection_context(); no ORM models are
rebound. Use one store per application database. Mutations must own their outer
transaction so publication always follows the actual database commit.
"""
from dataclasses import replace
from threading import Lock

from app.service.torque.types import (
    RuleSnapshot, TorqueConfig, TorqueImportChange, TorqueImportError,
    TorqueImportPreview, TorqueImportResult, TorqueImportRow,
)
from app.service.torque.validation import (
    normalize_key, parse_offset, resolve_addresses, validate_db_number,
    validate_match_type, validate_torque,
)


class TorquePreviewConflict(ValueError):
    """Rules changed after preview; the caller must generate a new preview."""


def _rule(match_type, match_key, value):
    return validate_match_type(match_type), normalize_key(match_key), validate_torque(value)


def _validated_rows(rows, errors=()):
    valid, problems, seen = [], list(errors), {}
    for row in rows:
        try:
            kind, key, value = _rule(row.match_type, row.match_key, row.torque_value)
            identity = kind, key
            if identity in seen:
                raise ValueError(f'匹配键重复（首次位于第 {seen[identity]} 行）')
            seen[identity] = row.row_number
            valid.append(TorqueImportRow(row.row_number, kind, key, value))
        except ValueError as exc:
            problems.append(TorqueImportError(row.row_number, str(exc)))
    return tuple(valid), tuple(problems)


class TorqueStore:
    def __init__(self, database):
        self.database = database
        self._mutation_lock = Lock()
        self._state_lock = Lock()
        values = {}
        for kind, key, value in database.execute_sql(
            'SELECT match_type, match_key, torque_value FROM torque_rule_table'
        ).fetchall():
            kind, key, value = _rule(kind, key, value)
            if (kind, key) in values:
                raise ValueError('持久化扭矩表包含重复匹配键')
            values[kind, key] = value
        self._snapshot = RuleSnapshot(0, values)
        self._configs = {}
        for row in database.execute_sql(
            'SELECT plc_id, enabled, match_type, read_db_number, read_byte_offset, '
            'write_db_number, write_byte_offset FROM plc_torque_config_table'
        ).fetchall():
            config = TorqueConfig(row[0], bool(row[1]), *row[2:])
            self._configs[config.plc_id] = config

    def snapshot(self):
        with self._state_lock:
            return self._snapshot

    def get_config(self, plc_id):
        with self._state_lock:
            return self._configs.get(plc_id, TorqueConfig(plc_id))

    def _require_outer_transaction(self):
        if self.database.in_transaction():
            raise RuntimeError('TorqueStore mutations must own their database transaction')

    def save_config(self, config, inherited_db):
        """Save and return a normalized config; inherited_db is the applied PLC DB.

        Verify PLC existence inside the transaction to reject jobs queued before
        deletion. A successful save does not apply the config to a PLC thread.
        """
        if isinstance(config.plc_id, bool) or not isinstance(config.plc_id, int) or config.plc_id <= 0:
            raise ValueError('PLC ID 必须为正整数')
        if not isinstance(config.enabled, bool):
            raise ValueError('启用设置必须为布尔值')
        config = replace(
            config, match_type=validate_match_type(config.match_type),
            read_db_number=validate_db_number(config.read_db_number),
            write_db_number=validate_db_number(config.write_db_number),
            read_byte_offset=None if config.read_byte_offset is None else parse_offset(config.read_byte_offset),
            write_byte_offset=None if config.write_byte_offset is None else parse_offset(config.write_byte_offset),
        )
        resolve_addresses(config, inherited_db)
        with self._mutation_lock:
            self._require_outer_transaction()
            config = replace(config, revision=self.get_config(config.plc_id).revision + 1)
            configs = dict(self._configs)
            configs[config.plc_id] = config
            with self.database.atomic('IMMEDIATE'):
                exists = self.database.execute_sql('SELECT 1 FROM plc_table WHERE id = ?', (config.plc_id,)).fetchone()
                if not exists:
                    raise ValueError('PLC 已删除或不存在')
                self.database.execute_sql(
                    'INSERT INTO plc_torque_config_table '
                    '(plc_id, enabled, match_type, read_db_number, read_byte_offset, write_db_number, write_byte_offset) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(plc_id) DO UPDATE SET '
                    'enabled=excluded.enabled, match_type=excluded.match_type, '
                    'read_db_number=excluded.read_db_number, read_byte_offset=excluded.read_byte_offset, '
                    'write_db_number=excluded.write_db_number, write_byte_offset=excluded.write_byte_offset',
                    (config.plc_id, config.enabled, config.match_type, config.read_db_number,
                     config.read_byte_offset, config.write_db_number, config.write_byte_offset),
                )
            with self._state_lock:
                self._configs = configs
            return config

    def delete_config(self, plc_id):
        """Remove only this PLC's torque config; retain a disabled revision receipt."""
        with self._mutation_lock:
            self._require_outer_transaction()
            config = TorqueConfig(plc_id, revision=self.get_config(plc_id).revision + 1)
            configs = dict(self._configs)
            configs[plc_id] = config
            with self.database.atomic('IMMEDIATE'):
                self.database.execute_sql('DELETE FROM plc_torque_config_table WHERE plc_id = ?', (plc_id,))
            with self._state_lock:
                self._configs = configs
            return config

    def delete_plc(self, plc_id):
        """Delete the PLC and both configurations in one owned transaction."""
        with self._mutation_lock:
            self._require_outer_transaction()
            config = TorqueConfig(plc_id, revision=self.get_config(plc_id).revision + 1)
            configs = dict(self._configs)
            configs[plc_id] = config
            with self.database.atomic('IMMEDIATE'):
                self.database.execute_sql('DELETE FROM plc_field_offset_table WHERE plc_id = ?', (plc_id,))
                self.database.execute_sql('DELETE FROM plc_torque_config_table WHERE plc_id = ?', (plc_id,))
                cursor = self.database.execute_sql('DELETE FROM plc_table WHERE id = ?', (plc_id,))
                if cursor.rowcount != 1:
                    raise ValueError('PLC 已删除或不存在')
            with self._state_lock:
                self._configs = configs
            return config

    def _publish_rules(self, values, writes=(), deletes=()):
        """Called under mutation lock; prepare before transaction, publish after commit."""
        self._require_outer_transaction()
        current = self.snapshot()
        if values == current.values:
            return current
        prepared = RuleSnapshot(current.revision + 1, values)
        with self.database.atomic('IMMEDIATE'):
            for kind, key in deletes:
                self.database.execute_sql('DELETE FROM torque_rule_table WHERE match_type = ? AND match_key = ?', (kind, key))
            for kind, key, value in writes:
                self.database.execute_sql(
                    'INSERT INTO torque_rule_table (match_type, match_key, torque_value) VALUES (?, ?, ?) '
                    'ON CONFLICT(match_type, match_key) DO UPDATE SET torque_value=excluded.torque_value',
                    (kind, key, value),
                )
        with self._state_lock:
            self._snapshot = prepared
        return prepared

    def create_rule(self, match_type, match_key, torque_value):
        return self._save_rule(match_type, match_key, torque_value, create=True)

    def upsert_rule(self, match_type, match_key, torque_value):
        return self._save_rule(match_type, match_key, torque_value, create=False)

    def _save_rule(self, match_type, match_key, torque_value, *, create):
        kind, key, value = _rule(match_type, match_key, torque_value)
        with self._mutation_lock:
            values = dict(self.snapshot().values)
            if create and (kind, key) in values:
                raise ValueError('匹配键已存在')
            values[kind, key] = value
            return self._publish_rules(values, [(kind, key, value)])

    def update_rule(self, old_type, old_key, match_type, match_key, torque_value):
        old = validate_match_type(old_type), normalize_key(old_key)
        kind, key, value = _rule(match_type, match_key, torque_value)
        with self._mutation_lock:
            values = dict(self.snapshot().values)
            if old not in values:
                raise ValueError('原匹配键不存在')
            if old != (kind, key) and (kind, key) in values:
                raise ValueError('匹配键已存在')
            del values[old]
            values[kind, key] = value
            return self._publish_rules(values, [(kind, key, value)], [old])

    def delete_rule(self, match_type, match_key):
        identity = validate_match_type(match_type), normalize_key(match_key)
        with self._mutation_lock:
            values = dict(self.snapshot().values)
            values.pop(identity, None)
            return self._publish_rules(values, deletes=[identity])

    def preview_import(self, rows):
        """Accept parsed result or import rows; errors prevent all later submission."""
        current = self.snapshot()
        result = rows if isinstance(rows, TorqueImportResult) else TorqueImportResult(rows)
        valid, errors = _validated_rows(result.rows, result.errors)
        additions, overwrites = [], []
        for row in valid:
            identity = row.match_type, row.match_key
            if identity in current.values:
                overwrites.append(TorqueImportChange(row, current.values[identity]))
            else:
                additions.append(row)
        return TorqueImportPreview(current.revision, valid, tuple(additions), tuple(overwrites), errors)

    def commit_import(self, preview):
        with self._mutation_lock:
            current = self.snapshot()
            if preview.revision != current.revision:
                raise TorquePreviewConflict('扭矩表已变化，请重新生成导入预览')
            rows, errors = _validated_rows(preview.rows, preview.errors)
            if errors:
                raise ValueError('导入包含错误，不能提交')
            values = dict(current.values)
            writes = []
            for row in rows:
                values[row.match_type, row.match_key] = row.torque_value
                writes.append((row.match_type, row.match_key, row.torque_value))
            return self._publish_rules(values, writes)
