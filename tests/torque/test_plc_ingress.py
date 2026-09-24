from dataclasses import replace
import struct
import threading
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot

from app.service.plc_service import PlcService
from app.service.torque.types import RuleSnapshot, TorqueConfig


class Signal:
    def __init__(self):
        self.events = []
    def emit(self, *args):
        self.events.append(args)


class Client:
    def __init__(self, order, raw=struct.pack('>f', 0)):
        self.order, self.raw = order, raw
        self.reads, self.writes, self.threads = [], [], []
        self.read_error = self.write_error = None
    def db_read(self, db, offset, size):
        self.order.append('read')
        self.reads.append((db, offset, size))
        self.threads.append(threading.get_ident())
        if self.read_error:
            raise self.read_error
        return self.raw
    def db_write(self, db, offset, data):
        self.order.append('write')
        self.writes.append((db, offset, bytes(data)))
        self.threads.append(threading.get_ident())
        if self.write_error:
            raise self.write_error


@pytest.fixture
def setup_service(qapp):
    order, logs, events, starts = [], [], [], []
    config = TorqueConfig(7, True, 'model', 2, 20, 3, 40, revision=4)
    store = SimpleNamespace(snapshot=lambda: RuleSnapshot(9, {('model', 'M100'): 2.5}), get_config=lambda plc_id: config)
    def start(*args):
        order.append('start')
        starts.append(args)
    bus = SimpleNamespace(log=Signal(), plc_workorder_changed=Signal())
    service = PlcService(bus, start, 'fake', plc_id=7, field_offsets_dict={'start_signal_offset': '0.0'}, torque_store=store, service_generation='generation-7')
    service.logger = SimpleNamespace(info=logs.append, error=logs.append)
    service.plc_client = Client(order)
    service._poll_current_value = lambda: None
    service._write_testing_signal = lambda value: None
    service._emit_plc_workorder = lambda: {'sn': '000123', 'model': 'M100'}
    service.write_plc_value = lambda *args: order.append('reset')
    service._last_value = False
    service._read_start_bit = lambda: True
    service.torque_status.connect(events.append)
    return SimpleNamespace(service=service, store=store, order=order, logs=logs, events=events, starts=starts, config=config)


def test_start_then_reset_then_real_io_with_explicit_db(setup_service):
    x = setup_service
    x.service.poll_once()
    assert x.order == ['start', 'reset', 'read', 'write']
    assert x.service.plc_client.reads == [(2, 20, 4)]
    assert x.service.plc_client.writes == [(3, 40, struct.pack('>f', 2.5))]
    assert x.service._db_number == 1
    assert x.events[-1].trigger_ns == x.starts[0][2]
    assert x.events[-1].state == 'table_sent'
    x.service.poll_once()
    assert len(x.service.plc_client.reads) == 1


@pytest.mark.parametrize('raw', [b'', b'\x00\x00\x00', b'\x00' * 5])
def test_short_or_long_real_is_read_error_without_table_fallback(setup_service, raw):
    x = setup_service
    x.service.plc_client.raw = raw
    x.service.poll_once()
    assert x.events[-1].state == 'read_error'
    assert x.events[-1].read_value is None
    assert x.service.plc_client.writes == []
    assert len(x.starts) == 1


def test_reset_failure_still_processes_torque_once(setup_service):
    x = setup_service
    def reset(*args):
        x.order.append('reset')
        raise OSError('reset offline')
    x.service.write_plc_value = reset
    x.service.poll_once()
    assert x.order == ['start', 'reset', 'read', 'write']
    assert x.events[-1].state == 'table_sent'
    assert any('reset offline' in message for message in x.logs)
    assert any('reset offline' in entry[0] for entry in x.service.bus.log.events)


def test_callback_failure_keeps_reset_but_skips_torque(setup_service):
    x = setup_service
    def start(*args):
        x.order.append('start')
        raise RuntimeError('start failed')
    x.service._on_start_requested = start
    x.service.poll_once()
    assert x.order == ['start', 'reset']
    assert x.events == []


def test_disabled_and_initial_high_have_no_torque_io(setup_service):
    x = setup_service
    x.service._last_value = None
    x.service.poll_once()
    assert x.order == []
    x.service.apply_torque_config(TorqueConfig(7, revision=x.config.revision + 1))
    x.service._last_value = False
    x.service.poll_once()
    assert x.order == ['start', 'reset']
    assert x.events == []


def test_cycle_freezes_config_rules_and_workorder_before_start(setup_service):
    x = setup_service
    original = x.service._on_start_requested
    def start(*args):
        original(*args)
        args[1]['model'] = 'CHANGED'
        x.store.snapshot = lambda: RuleSnapshot(10, {('model', 'M100'): 8.0})
        x.service.apply_torque_config(replace(x.config, write_db_number=4, revision=5))
    x.service._on_start_requested = start
    x.service.poll_once()
    assert x.service.plc_client.writes == [(3, 40, struct.pack('>f', 2.5))]
    assert x.events[-1].rules_revision == 9
    x.service._on_start_requested = original
    x.service._last_value = False
    x.service.poll_once()
    assert x.service.plc_client.writes[-1] == (4, 40, struct.pack('>f', 8.0))


def test_config_receipt_filters_plc_and_checks_applied_db_without_io(setup_service):
    x = setup_service
    receipts = []
    x.service.torque_config_applied.connect(receipts.append)
    x.service.apply_torque_config(replace(x.config, plc_id=8))
    assert receipts == []
    config = replace(x.config, read_db_number=None, write_db_number=1, write_byte_offset=22)
    x.service.apply_torque_config(config)
    receipt = receipts[-1]
    assert (receipt.plc_id, receipt.service_generation, receipt.config_revision, receipt.success) == (7, 'generation-7', 4, False)
    assert x.order == []
    x.service._db_number = 2
    x.service.apply_torque_config(config)
    assert receipts[-1].success
    x.service._db_number = 1  # reconnect/base DB changed: revalidate each cycle
    x.service.poll_once()
    assert x.events[-1].state == 'config_error'
    assert x.order == ['start', 'reset']


def test_queued_configuration_and_real_io_run_on_plc_owner_thread(setup_service, qapp):
    x = setup_service
    caller_thread = threading.get_ident()
    owner_thread_ids = []
    receipts = []
    x.service.torque_config_applied.connect(receipts.append)

    class Requests(QObject):
        config = pyqtSignal(object)
        poll = pyqtSignal()

    class Driver(QObject):
        @pyqtSlot()
        def poll(self):
            try:
                owner_thread_ids.append(threading.get_ident())
                x.service.poll_once()
            finally:
                x.service.moveToThread(qapp.thread())
                self.moveToThread(qapp.thread())
                thread.quit()

    thread = QThread()
    driver, requests = Driver(), Requests()
    x.service.moveToThread(thread)
    driver.moveToThread(thread)
    requests.config.connect(x.service.apply_torque_config, Qt.ConnectionType.QueuedConnection)
    requests.poll.connect(driver.poll, Qt.ConnectionType.QueuedConnection)
    thread.start()
    try:
        requests.config.emit(replace(x.config, write_db_number=4, revision=5))
        requests.poll.emit()
        assert thread.wait(3000)
        qapp.processEvents()
        assert owner_thread_ids[0] != caller_thread
        assert x.service.plc_client.threads == owner_thread_ids * 2
        assert x.service.plc_client.writes == [(4, 40, struct.pack('>f', 2.5))]
        assert receipts[-1].config_revision == 5
        assert receipts[-1].service_generation == 'generation-7'
    finally:
        thread.quit()
        thread.wait(3000)


def test_reset_disconnect_reports_read_error_without_retry(setup_service):
    x = setup_service
    def reset(*args):
        x.order.append('reset')
        x.service.plc_client = None
        raise OSError('connection lost')
    x.service.write_plc_value = reset
    x.service.poll_once()
    assert x.order == ['start', 'reset']
    assert x.events[-1].state == 'read_error'
    assert x.events[-1].read_value is None


def test_nonzero_same_real_is_valid_on_two_actual_rising_edges(setup_service):
    x = setup_service
    x.service.plc_client.raw = struct.pack('>f', 3.0)
    values = iter([True, True, False, True])
    x.service._read_start_bit = lambda: next(values)
    for _ in range(4):
        x.service.poll_once()
    assert len(x.starts) == 2
    assert x.service.plc_client.reads == [(2, 20, 4)] * 2
    assert x.service.plc_client.writes == []
    assert [e.value for e in x.events if e.state == 'plc'] == [3.0, 3.0]


@pytest.mark.parametrize('state, raw_value, source, written, error', [
    ('plc', 3.0, 'plc', None, ''),
    ('table_sent', 0.0, 'table', 2.5, ''),
    ('unmatched', 0.0, None, None, ''),
    ('read_error', None, None, None, 'read offline'),
    ('write_error', 0.0, 'table', 2.5, 'write offline'),
    ('config_error', None, None, None, '重叠'),
])
def test_terminal_logs_include_complete_cycle_context(setup_service, state, raw_value, source, written, error):
    x = setup_service
    if state == 'plc':
        x.service.plc_client.raw = struct.pack('>f', 3)
    elif state == 'unmatched':
        x.store.snapshot = lambda: RuleSnapshot(9, {})
    elif state == 'read_error':
        x.service.plc_client.read_error = OSError(error)
    elif state == 'write_error':
        x.service.plc_client.write_error = OSError(error)
    elif state == 'config_error':
        x.service.apply_torque_config(replace(x.config, write_db_number=2, write_byte_offset=22))
    x.service.poll_once()
    assert x.events[-1].state == state
    line = next(message for message in x.logs if f'state={state}' in message)
    for expected in ('plc_id=7', f'trigger_ns={x.starts[0][2]}', "sn='000123'", "model='M100'", 'rules_revision=9', f'read_value={raw_value!r}', f'source={source!r}', 'target_db=', 'target_byte_offset=', f'write_value={written!r}', 'error=', error):
        assert expected in line
    if error:
        assert any(line == entry[0] for entry in x.service.bus.log.events)
    assert len(x.starts) == 1
