"""Real migration/import/Qt integration with an in-memory PLC transport only."""
import sqlite3
import struct
import threading
import time
from itertools import count
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from peewee import SqliteDatabase
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot

import app.service.db_connection_service as database_service
import app.service.plc_service as plc_module
from app.service.plc_service import PlcService
from app.service.torque.jobs import TorqueConfigCoordinator, TorqueJobs
from app.service.torque.store import TorqueStore
from app.service.torque.types import TorqueConfig
from app.services_container import EventBus, FactoryControllerState
from model.plc_table import PlcTable
from tests.test_db_connection_service import _create_runtime_tables
from tests.torque.test_jobs import until
from ui.modules.workorder import WorkOrderModule


class MemoryPlc:
    """Exercise real Siemens byte decoding, start reset, and torque encoding."""

    def __init__(self):
        self.blocks = {2: bytearray(256), 3: bytearray(256)}
        self.order, self.reads, self.writes, self.threads = [], [], [], []
        self.read_error = self.write_error = False
        self.read_entered, self.release_read = threading.Event(), threading.Event()
        self.release_read.set()
        self.set_text(100, '000123')
        self.set_text(150, 'M100')

    def set_text(self, offset, text):
        raw = text.encode('ascii')
        self.blocks[2][offset:offset + len(raw) + 2] = bytes([36, len(raw)]) + raw

    def db_read(self, db, offset, size):
        self.threads.append(threading.get_ident())
        self.reads.append((db, offset, size))
        if (db, offset) == (2, 20):
            self.order.append('torque_read')
            self.read_entered.set()
            if not self.release_read.wait(5):
                raise TimeoutError('test did not release torque read')
            if self.read_error:
                raise OSError('fake torque read disconnected')
        return self.blocks[db][offset:offset + size]

    def db_write(self, db, offset, data):
        self.threads.append(threading.get_ident())
        self.writes.append((db, offset, bytes(data)))
        if (db, offset) == (2, 0):
            self.order.append('start_reset')
        if (db, offset) == (3, 40):
            self.order.append('torque_write')
            if self.write_error:
                raise OSError('fake torque write disconnected')
        self.blocks[db][offset:offset + len(data)] = data

    @property
    def torque_writes(self):
        return [write for write in self.writes if write[:2] == (3, 40)]


class Requests(QObject):
    run = pyqtSignal(object)
    stop = pyqtSignal()


class PollDriver(QObject):
    finished = pyqtSignal(object)

    def __init__(self, service, ui_thread):
        super().__init__()
        self.service, self.ui_thread = service, ui_thread

    @pyqtSlot(object)
    def run(self, action):
        try:
            action()
        except Exception as error:
            self.finished.emit(error)
        else:
            self.finished.emit(None)

    @pyqtSlot()
    def stop(self):
        owner = QThread.currentThread()
        self.service.moveToThread(self.ui_thread)
        self.moveToThread(self.ui_thread)
        owner.quit()


class Harness:
    def job(self, operation, *args):
        request_id = self.jobs.submit(operation, *args)
        until(self.app, lambda: any(r.request_id == request_id for r in self.results))
        result = next(r for r in self.results if r.request_id == request_id)
        assert result.error is None and not result.cancelled
        return result.value

    def import_rules(self, value):
        book = Workbook()
        sheet = book.active
        sheet.append(['匹配类型', '型号或SN', '扭矩'])
        sheet.append(['型号', 'M100', value])
        sheet.append(['SN', '000123', 4.5])
        sheet['B3'].number_format = '@'
        path = self.path.parent / 'rules.xlsx'
        book.save(path)
        book.close()
        preview = self.job('parse_import', path)
        assert not preview.errors
        self.job('commit_import', preview)
        return preview

    def begin_cycle(self, value=0.0, model='M100'):
        def poll():
            self.client.blocks[2][20:24] = struct.pack('>f', value)
            self.client.set_text(150, model)
            self.client.blocks[2][0] = 0
            self.service.poll_once()
            self.client.blocks[2][0] = 1
            self.service.poll_once()
        self.client.order.clear()
        self.completed.clear()
        self.requests.run.emit(poll)

    def finish_cycle(self, expected_state):
        until(self.app, lambda: bool(self.completed))
        assert self.completed == [None]
        trigger = self.starts[-1][2]
        until(self.app, lambda: bool(self.statuses) and
              self.statuses[-1].trigger_ns == trigger and
              self.statuses[-1].state == expected_state)
        assert self.module._plc_torque_statuses[7] == self.statuses[-1]
        assert self.client.order[:3] == ['start_submitted', 'start_reset', 'torque_read']
        assert self.client.blocks[2][0] == 0
        return self.statuses[-1]

    @property
    def display(self):
        return self.module._plc_torque_edits[7].text()


@pytest.fixture
def integrated(tmp_path, monkeypatch, qapp):
    x = Harness()
    # Direct test polls run faster than Windows' 15.625 ms monotonic clock.
    # Model production-spaced starts without sleeping or changing capture time.
    ticks = count(1_000_000_000, 50_000_000)
    clock = dict(vars(time), monotonic_ns=lambda: next(ticks))
    monkeypatch.setattr(plc_module, 'time', SimpleNamespace(**clock))
    x.app, x.path = qapp, tmp_path / 'old-production.db'
    x.database = SqliteDatabase(x.path)
    x.database.connect()
    _create_runtime_tables(x.database)
    x.database.execute_sql('INSERT INTO test_config_table (id) VALUES (17)')
    x.database.execute_sql('ALTER TABLE test_config_table DROP COLUMN manual_judgment_buttons_visible')
    with PlcTable.bind_ctx(x.database, bind_refs=False, bind_backrefs=False):
        # The existing schema helper has minimal PLC columns; use the legacy ORM.
        x.database.drop_tables([PlcTable])
        x.database.create_tables([PlcTable])
        PlcTable.create(id=7, plc_name='PLC 7', plc_db_number=2)
        monkeypatch.setattr(database_service, 'db', x.database)
        monkeypatch.setattr(database_service, 'DB_PATH', str(x.path))
        database_service.DbConnectionService().connect()
        assert x.database.execute_sql(
            'SELECT id, manual_judgment_buttons_visible FROM test_config_table'
        ).fetchall() == [(17, 0)]
        backups = list(tmp_path.glob('old-production.db.backup-auto-*'))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute('SELECT id FROM test_config_table').fetchall() == [(17,)]
            assert not backup.execute(
                "SELECT name FROM sqlite_master WHERE name = 'torque_rule_table'"
            ).fetchall()
        x.store = TorqueStore(x.database)
        assert x.store.get_config(7) == TorqueConfig(7)
        x.jobs, x.bus = TorqueJobs(x.store), EventBus()
        x.results, x.starts, x.statuses, x.completed = [], [], [], []
        x.jobs.finished.connect(x.results.append)
        x.bus.plc_torque_changed.connect(x.statuses.append)
        x.client = MemoryPlc()

        def start(*args):
            x.client.order.append('start_submitted')
            x.starts.append(args)

        x.service = PlcService(
            x.bus, start, 'fake-no-network', plc_id=7, db_number=2,
            field_offsets_dict={'start_signal_offset': '0.0',
                                'sn_code_offset': '100.0', 'product_model_offset': '150.0',
                                'rotation_speed_offset': '200.0'}, torque_store=x.store)
        x.service.plc_client = x.client
        services = SimpleNamespace(
            bus=x.bus, factory_mode=SimpleNamespace(state=FactoryControllerState()),
            plc_services={7: x.service}, plc_threads={}, plc_state={}, torque_store=x.store)
        x.coordinator = TorqueConfigCoordinator(x.store, x.jobs, x.bus, services.plc_services)
        x.coordinator.bind_service(7, x.service)
        x.module = WorkOrderModule(services)
        x.module.show()
        x.thread, x.requests = QThread(), Requests()
        x.driver = PollDriver(x.service, qapp.thread())
        x.driver.finished.connect(x.completed.append)
        x.service.moveToThread(x.thread)
        x.driver.moveToThread(x.thread)
        x.requests.run.connect(x.driver.run, Qt.ConnectionType.QueuedConnection)
        x.requests.stop.connect(x.driver.stop, Qt.ConnectionType.QueuedConnection)
        x.thread.start()
        try:
            x.import_rules(2.5)
            x.job('save_config', TorqueConfig(7, True, 'model', None, 20, 3, 40), 2)
            until(qapp, lambda: x.coordinator.get_applied_receipt(7) is not None)
            assert x.coordinator.get_applied_receipt(7).success
            assert x.display == '待读取'
            assert not x.client.reads and not x.client.writes
            yield x
        finally:
            x.client.release_read.set()
            x.requests.stop.emit()
            assert x.thread.wait(6000)
            x.jobs.close()
            x.module.close()
            x.module.deleteLater()
            qapp.processEvents()
            x.database.close()


def test_zero_imported_model_writes_real_and_routes_to_workorder(integrated):
    x = integrated
    x.begin_cycle()
    status = x.finish_cycle('table_sent')
    assert x.starts[0][:2] == (7, {'sn': '000123', 'model': 'M100', 'rotation_speed': 0})
    assert x.client.torque_writes == [(3, 40, struct.pack('>f', 2.5))]
    assert bytes(x.client.blocks[3][40:44]) == b'\x40\x20\x00\x00'
    assert x.client.reads.count((2, 20, 4)) == 1
    assert len(set(x.client.threads)) == 1
    assert x.client.threads[0] != threading.get_ident()
    assert (status.source, status.value) == ('table', 2.5)
    assert x.display == '2.5（软件表，已回传）'
    assert x.module._plc_sn_edits[7].text() == '000123'
    assert x.module._plc_model_edits[7].text() == 'M100'
    reloaded = TorqueStore(x.database)
    assert reloaded.get_config(7).enabled
    assert dict(reloaded.snapshot().values) == {('model', 'M100'): 2.5, ('sn', '000123'): 4.5}


@pytest.mark.parametrize('model', ['M100', 'absent'])
def test_persistent_nonzero_plc_value_is_valid_on_every_start(integrated, model):
    x = integrated
    triggers = []
    for _ in range(3):
        x.begin_cycle(3.0, model)
        status = x.finish_cycle('plc')
        triggers.append(status.trigger_ns)
        assert (status.source, status.value) == ('plc', 3.0)
        assert x.display == '3（PLC）'
    assert len(x.starts) == len(set(triggers)) == 3
    assert x.client.reads.count((2, 20, 4)) == 3
    assert not x.client.torque_writes


@pytest.mark.parametrize('failure, expected', [
    ('read_error', '读取失败'), ('unmatched', '未匹配（型号：absent）'),
    ('write_error', '2.5（软件表，回传失败）'),
])
def test_failures_preserve_submitted_start_and_replace_old_success(integrated, failure, expected):
    x = integrated
    x.begin_cycle()
    previous = x.finish_cycle('table_sent')
    assert x.display == '2.5（软件表，已回传）'
    x.client.read_error = failure == 'read_error'
    x.client.write_error = failure == 'write_error'
    x.begin_cycle(model='absent' if failure == 'unmatched' else 'M100')
    status = x.finish_cycle(failure)
    assert len(x.starts) == 2 and status.trigger_ns > previous.trigger_ns
    assert x.display == expected
    assert '已回传' not in x.module._plc_torque_edits[7].toolTip()
    assert len(x.client.torque_writes) == (2 if failure == 'write_error' else 1)


def test_background_import_during_plc_read_changes_only_next_cycle(integrated):
    x = integrated
    old_revision = x.store.snapshot().revision
    x.client.release_read.clear()
    x.begin_cycle()
    until(x.app, x.client.read_entered.is_set)
    assert len(x.starts) == 1
    until(x.app, lambda: x.display == '读取中')
    preview = x.import_rules(7.5)
    assert preview.overwrites[0].old_value == 2.5
    assert x.store.snapshot().revision > old_revision
    assert not x.completed and not x.client.torque_writes
    assert x.display == '读取中'
    x.client.release_read.set()
    assert x.finish_cycle('table_sent').rules_revision == old_revision
    assert x.display == '2.5（软件表，已回传）'
    x.begin_cycle()
    assert x.finish_cycle('table_sent').rules_revision == x.store.snapshot().revision
    assert x.display == '7.5（软件表，已回传）'
    assert x.client.torque_writes == [(3, 40, struct.pack('>f', value)) for value in (2.5, 7.5)]


def test_equal_acoustic_timestamps_correlate_new_cycles_across_reconnect(integrated, monkeypatch):
    x = integrated
    monkeypatch.setattr(plc_module.time, 'monotonic_ns', lambda: 123_000_000)
    x.begin_cycle()
    first = x.finish_cycle('table_sent')
    x.client.read_error = True
    x.begin_cycle()
    second = x.finish_cycle('read_error')
    assert x.display == '读取失败'
    assert first.trigger_ns == second.trigger_ns == x.starts[0][2] == x.starts[1][2]
    assert first.cycle_id < second.cycle_id

    # A queued predecessor must not resurrect its success after the new failure.
    old_service = x.service
    old_service.torque_status.emit(first)
    until(x.app, lambda: x.statuses[-1] is first)
    assert x.display == '读取失败'

    def recreate_service():
        old_service.moveToThread(x.app.thread())
        x.service = PlcService(
            x.bus, old_service._on_start_requested, 'fake-reconnected', plc_id=7,
            db_number=2, field_offsets_dict=old_service._field_offsets_dict,
            torque_store=x.store)
        x.service.plc_client = x.client
        x.driver.service = x.service

    x.completed.clear()
    x.requests.run.emit(recreate_service)
    until(x.app, lambda: bool(x.completed))
    assert x.completed == [None]
    x.coordinator.services[7] = x.service
    x.coordinator.bind_service(7, x.service)
    assert x.display == '读取失败'
    x.client.read_error = False
    x.begin_cycle(3.0)
    third = x.finish_cycle('plc')
    assert third.trigger_ns == 123_000_000 == x.starts[2][2]
    assert third.cycle_id > second.cycle_id
    assert x.display == '3（PLC）'
    old_service.torque_status.emit(second)
    until(x.app, lambda: x.statuses[-1] is second)
    assert x.display == '3（PLC）'
