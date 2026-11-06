import struct
import threading
from contextlib import contextmanager
from dataclasses import replace

from PyQt6.QtCore import Q_ARG, QMetaObject, QObject, QThread, Qt, pyqtSignal, pyqtSlot

from app.service.plc_service import PlcService
from app.service.torque.jobs import TorqueConfigCoordinator
from app.service.torque.types import TorqueConfig
from app.services_container import EventBus
from tests.torque.test_jobs import job_env, until


def test_save_applies_on_plc_thread_and_next_start_reads_fresh_rules(job_env, qapp):
    jobs, store, db, results = job_env
    bus = EventBus()
    calls, statuses, receipts = [], [], []
    service = PlcService(bus, lambda *args: None, 'fake', plc_id=7,
                         field_offsets_dict={'start_signal_offset': '0.0'},
                         torque_store=store, torque_config=store.get_config(7), service_generation='new')
    class Client:
        def db_read(self, *args):
            calls.append(('read', threading.get_ident()))
            return struct.pack('>f', 0)
        def db_write(self, db, offset, data):
            calls.append((struct.unpack('>f', data)[0], threading.get_ident()))
    service.plc_client = Client()
    service._poll_current_value = lambda: None
    service._write_testing_signal = lambda value: None
    service._emit_plc_workorder = lambda: {'model': 'M100', 'sn': '000123'}
    service.write_plc_value = lambda *args: None
    service._read_start_bit = lambda: True
    class Driver(QObject):
        run = pyqtSignal()
    class Cycle(QObject):
        @pyqtSlot()
        def run(self):
            service._last_value = False
            service.poll_once()
    owner = QThread()
    cycle, driver = Cycle(), Driver()
    cycle.moveToThread(owner)
    service.moveToThread(owner)
    driver.run.connect(cycle.run, Qt.ConnectionType.QueuedConnection)
    owner.finished.connect(service.deleteLater)
    owner.finished.connect(cycle.deleteLater)
    services = {7: service}
    coordinator = TorqueConfigCoordinator(store, jobs, bus, services)
    coordinator.bind_service(7, service)
    bus.plc_torque_changed.connect(statuses.append)
    bus.plc_torque_config_applied.connect(receipts.append)
    owner.start()
    try:
        jobs.submit('save_config', TorqueConfig(7, True, 'model', 1, 20, 1, 24), 1)
        jobs.submit('upsert_rule', 'model', 'M100', 2.5)
        until(qapp, lambda: receipts and len(results) == 2)
        assert not calls
        old = store.snapshot()
        driver.run.emit()
        until(qapp, lambda: statuses and statuses[-1].state == 'table_sent')
        jobs.submit('upsert_rule', 'model', 'M100', 3.5)
        until(qapp, lambda: len(results) == 3)
        assert old.values[('model', 'M100')] == 2.5
        count = len(statuses)
        driver.run.emit()
        until(qapp, lambda: len(statuses) == count + 3)
        assert [entry[0] for entry in calls] == ['read', 2.5, 'read', 3.5]
        assert len({entry[1] for entry in calls}) == 1
        assert calls[0][1] != threading.get_ident()
        assert statuses[-1].rules_revision == old.revision + 1
    finally:
        services.clear()
        owner.quit()
        assert owner.wait(3000)


@contextmanager
def running_config_service(store, bus, config):
    """A real PLC owner thread whose fake client records actual runtime addresses."""
    service = PlcService(bus, lambda *args: None, 'fake', plc_id=7,
                         field_offsets_dict={'start_signal_offset': '0.0'},
                         torque_store=store, torque_config=config, service_generation='replacement')
    calls = []
    class Client:
        def db_read(self, db, offset, size):
            calls.append(('read', db, offset, threading.get_ident()))
            return struct.pack('>f', 0)
        def db_write(self, db, offset, data):
            calls.append(('write', db, offset, threading.get_ident()))
    service.plc_client = Client()
    service._poll_current_value = lambda: None
    service._write_testing_signal = lambda value: None
    service._emit_plc_workorder = lambda: {'model': 'M100'}
    service.write_plc_value = lambda *args: None
    service._read_start_bit = lambda: True
    class Probe(QObject):
        config = None
        @pyqtSlot()
        def poll(self):
            self.config = service._torque_cycle(1, {'model': 'M100'}).config
            service._last_value = False
            service.poll_once()
    owner, probe = QThread(), Probe()
    service.moveToThread(owner)
    probe.moveToThread(owner)
    owner.finished.connect(service.deleteLater)
    owner.finished.connect(probe.deleteLater)
    owner.start()
    try:
        yield service, probe, calls
    finally:
        owner.quit()
        assert owner.wait(3000)


def test_delayed_save_completion_cannot_roll_back_replacement(job_env, qapp, monkeypatch):
    jobs, store, db, results = job_env
    bus, services = EventBus(), {}
    coordinator = TorqueConfigCoordinator(store, jobs, bus, services)
    saved = []
    bus.plc_torque_config_saved.connect(saved.append)
    committed = threading.Event()
    original_save = store.save_config
    def save(*args):
        result = original_save(*args)
        committed.set()
        return result
    monkeypatch.setattr(store, 'save_config', save)
    jobs.submit('save_config', TorqueConfig(7, True, 'model', 1, 20, 1, 24), 1)
    assert committed.wait(3)  # Hold main-thread completion delivery until replacement exists.
    old = store.get_config(7)
    current = store.save_config(replace(old, read_byte_offset=40, write_byte_offset=44), 1)
    assert (old.revision, current.revision) == (1, 2)
    store.upsert_rule('model', 'M100', 2.5)
    with running_config_service(store, bus, current) as (service, probe, calls):
        services[7] = service
        coordinator.bind_service(7, service)
        until(qapp, lambda: len(results) == 1)
        QMetaObject.invokeMethod(probe, 'poll', Qt.ConnectionType.BlockingQueuedConnection)
        assert probe.config is current
        assert [call[:3] for call in calls] == [('read', 1, 40), ('write', 1, 44)]
        assert saved == []
        assert all(call[3] != threading.get_ident() for call in calls)
        services.clear()


def test_already_queued_old_config_cannot_roll_back_owner_and_equal_revision_receipts_remain(job_env, qapp):
    jobs, store, db, results = job_env
    bus = EventBus()
    old = store.save_config(TorqueConfig(7, True, 'model', 1, 20, 1, 24), 1)
    current = store.save_config(replace(old, read_byte_offset=40, write_byte_offset=44), 1)
    store.upsert_rule('model', 'M100', 2.5)
    receipts = []
    with running_config_service(store, bus, current) as (service, probe, calls):
        service.torque_config_applied.connect(receipts.append)
        QMetaObject.invokeMethod(service, 'apply_torque_config',
                                 Qt.ConnectionType.QueuedConnection, Q_ARG(object, old))
        QMetaObject.invokeMethod(probe, 'poll', Qt.ConnectionType.BlockingQueuedConnection)
        assert probe.config is current
        assert [call[:3] for call in calls] == [('read', 1, 40), ('write', 1, 44)]
        QMetaObject.invokeMethod(service, 'apply_torque_config',
                                 Qt.ConnectionType.BlockingQueuedConnection, Q_ARG(object, current))
        until(qapp, lambda: receipts)
        assert [receipt.config_revision for receipt in receipts] == [2]
        assert receipts[0].success
        assert len(calls) == 2  # Applying config itself never does torque I/O.
