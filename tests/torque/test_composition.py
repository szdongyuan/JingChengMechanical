from types import SimpleNamespace

import pytest

from PyQt6.QtCore import QObject, pyqtSignal

from app.service.torque.jobs import TorqueConfigCoordinator
from app.service.torque.types import TorqueConfig, TorqueConfigApplied
from app.services_container import EventBus, ServiceContainer
from tests.torque.test_jobs import job_env, until


class FakeService(QObject):
    torque_status = pyqtSignal(object)
    torque_config_applied = pyqtSignal(object)
    service_generation = 'new'


def test_offline_save_publishes_pending_and_old_receipts_are_filtered(job_env, qapp):
    jobs, store, db, results = job_env
    bus = EventBus()
    services = {}
    coordinator = TorqueConfigCoordinator(store, jobs, bus, services)
    saved, applied = [], []
    bus.plc_torque_config_saved.connect(saved.append)
    bus.plc_torque_config_applied.connect(applied.append)
    jobs.submit('save_config', TorqueConfig(7), 1)
    until(qapp, lambda: len(saved) == 1)
    assert applied == []
    assert coordinator.get_applied_receipt(7) is None
    service = FakeService()
    services[7] = service
    coordinator.bind_service(7, service)
    for generation, revision in [('old', 1), ('new', 0), ('new', 1)]:
        service.torque_config_applied.emit(TorqueConfigApplied(7, generation, revision, True))
    until(qapp, lambda: len(applied) == 1)
    assert applied[0].config_revision == saved[0].revision
    assert coordinator.get_applied_receipt(7) is applied[0]
    services.clear()
    service.torque_config_applied.emit(TorqueConfigApplied(7, 'new', 1, True))
    qapp.processEvents()
    assert len(applied) == 1
    assert coordinator.get_applied_receipt(7) is None


def test_container_initializes_store_from_configured_model_database(setup_test_db, monkeypatch, tmp_path):
    import app.services_container as module
    monkeypatch.setattr(module.DbConnectionService, 'connect', lambda self: None)
    monkeypatch.setattr(module, 'AiService', lambda: SimpleNamespace())
    container = ServiceContainer(run_persistence_path=tmp_path / 'runs.db')
    try:
        assert container.torque_store.database is setup_test_db
        assert container.torque_jobs.store is container.torque_store
        assert not container.torque_jobs.worker_thread.isRunning()
        assert container._shutdown_resources().application_config_writers == (container.torque_jobs,)
    finally:
        container.torque_jobs.close()
        if container.run_persistence_database:
            container.run_persistence_database.close_after_upstream_stopped()


def test_plc_settings_connect_passes_latest_saved_config_and_new_generation(job_env, qapp, monkeypatch):
    from PyQt6.QtCore import QThread, pyqtSlot
    from PyQt6.QtWidgets import QWidget
    import ui.modules.plc_settings as module
    jobs, store, db, results = job_env
    saved = store.save_config(TorqueConfig(7), 1)
    created = []
    class Service(FakeService):
        connect_result = pyqtSignal(bool, str)
        state_changed = pyqtSignal(str)
        def __init__(self, **kwargs):
            super().__init__()
            self.kwargs = kwargs
            self.service_generation = kwargs.get('service_generation')
            created.append(self)
        @pyqtSlot()
        def start_plc_service(self):
            pass
        def last_write_test_result_to_plc(self, *args):
            pass
        def last_write_limit_to_plc(self, *args):
            pass
    class Thread(QThread):
        def start(self):
            pass
    monkeypatch.setattr(module, 'PlcService', Service)
    monkeypatch.setattr(module, 'QThread', Thread)
    widget = module.PlcSettingsModule.__new__(module.PlcSettingsModule)
    QWidget.__init__(widget)
    widget.services = SimpleNamespace(bus=EventBus(), plc_services={}, plc_threads={}, plc_state={},
                                     torque_store=store, _start_from_plc=lambda *args: None)
    widget.services.torque_config_coordinator = TorqueConfigCoordinator(
        store, jobs, widget.services.bus, widget.services.plc_services)
    widget.logger = SimpleNamespace(info=lambda *args: None)
    widget.combo_plc = SimpleNamespace(currentData=lambda: None)
    widget.get_field_offsets = lambda plc_id: {}
    row = SimpleNamespace(id=7, plc_ip='fake', plc_rack=0, plc_slot=1, plc_db_number=1,
                          plc_poll_interval_ms=100, plc_reconnect_interval_ms=1000)
    widget.start_connect(row)
    assert created[-1].kwargs['torque_store'] is store
    assert created[-1].kwargs['torque_config'] is saved
    first_generation = created[-1].service_generation
    old_thread = widget.services.plc_threads[7]
    widget.services.plc_services.clear()
    widget.services.plc_threads.clear()
    latest = store.save_config(TorqueConfig(7, match_type='sn'), 1)
    widget.start_connect(row)
    assert created[-1].kwargs['torque_config'] is latest
    assert created[-1].service_generation != first_generation
    widget._on_connect_thread_stopped(7, old_thread)
    assert widget.services.plc_services[7] is created[-1]


@pytest.mark.parametrize('disconnect_succeeds', [True, False])
def test_plc_settings_deletion_is_background_and_requires_disconnected_owner(job_env, qapp, monkeypatch, disconnect_succeeds):
    from PyQt6.QtWidgets import QWidget
    import ui.modules.plc_settings as module
    jobs, store, db, results = job_env
    store.save_config(TorqueConfig(7), 1)
    widget = module.PlcSettingsModule.__new__(module.PlcSettingsModule)
    QWidget.__init__(widget)
    widget._torque_delete_requests = {}
    widget.services = SimpleNamespace(torque_jobs=jobs, plc_services={7: object()}, plc_threads={},
                                     plc_state={7: '已连接'}, bus=EventBus())
    widget.get_current_row = lambda: SimpleNamespace(id=7, plc_name='fake')
    widget.disconnect_row = lambda plc_id: widget.services.plc_services.clear() if disconnect_succeeds else None
    widget.btn_delete_plc = SimpleNamespace(setEnabled=lambda enabled: None)
    refreshes, messages = [], []
    widget.reload_rows = lambda: refreshes.append(True)
    widget._emit_plc_changed = lambda: None
    jobs.finished.connect(widget._on_torque_job_finished)
    monkeypatch.setattr(module.QMessageBox, 'question', lambda *args: module.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(module.QMessageBox, 'warning', lambda *args: messages.append('warning'))
    monkeypatch.setattr(module.QMessageBox, 'information', lambda *args: messages.append('success'))
    widget.delete_current_plc()
    if disconnect_succeeds:
        until(qapp, lambda: messages)
        assert messages == ['success']
        assert refreshes
        assert not db.execute_sql('SELECT * FROM plc_table').fetchall()
        assert not db.execute_sql('SELECT * FROM plc_torque_config_table').fetchall()
    else:
        assert messages == ['warning']
        assert not widget._torque_delete_requests
        assert db.execute_sql('SELECT id FROM plc_table').fetchone()[0] == 7


@pytest.fixture
def connected_settings(job_env, qapp, monkeypatch):
    import threading
    from PyQt6.QtWidgets import QLabel, QPushButton, QWidget
    import app.service.plc_service as plc_module
    import ui.modules.plc_settings as module
    jobs, store, db, results = job_env
    calls = []
    class Client:
        def connect(self, *args):
            calls.append(('connect', threading.get_ident()))
        def disconnect(self):
            calls.append(('disconnect', threading.get_ident()))
    monkeypatch.setattr(plc_module.snap7.client, 'Client', Client)
    widget = module.PlcSettingsModule.__new__(module.PlcSettingsModule)
    QWidget.__init__(widget)
    widget.services = SimpleNamespace(bus=EventBus(), plc_services={}, plc_threads={}, plc_state={},
                                     torque_store=store, _start_from_plc=lambda *args: None)
    widget.services.torque_config_coordinator = TorqueConfigCoordinator(
        store, jobs, widget.services.bus, widget.services.plc_services)
    widget.logger = SimpleNamespace(info=lambda *args: None, error=lambda *args: None)
    widget.combo_plc = SimpleNamespace(currentData=lambda: 7)
    widget.btn_connect = QPushButton()
    widget.lb_connect_state = QLabel()
    widget._disconnecting_threads = set()
    widget.state_change_threads = []
    set_state = widget.set_plc_state
    def record_state(plc_id, state):
        widget.state_change_threads.append(threading.get_ident())
        set_state(plc_id, state)
    widget.set_plc_state = record_state
    widget.get_field_offsets = lambda plc_id: {}
    row = SimpleNamespace(id=7, plc_ip='fake', plc_rack=0, plc_slot=1, plc_db_number=1,
                          plc_poll_interval_ms=60000, plc_reconnect_interval_ms=1000)
    widget.get_current_row = lambda: row
    yield widget, row, calls
    # Tests restore temporary failure injections before this real shutdown.
    for plc_id in tuple(widget.services.plc_threads):
        widget.disconnect_row(plc_id)
    qapp.processEvents()


def test_real_settings_disconnect_reconnect_updates_state_before_late_callbacks(connected_settings, qapp):
    import threading
    widget, row, calls = connected_settings
    widget.start_connect(row)
    until(qapp, lambda: widget.get_plc_state(7) == '已连接')
    old_service, old_thread = widget.services.plc_services[7], widget.services.plc_threads[7]
    old_generation = old_service.service_generation
    widget.disconnect_row(7)
    # Main-thread wait prevented delivery of the old queued 未连接/finished.
    assert not old_thread.isRunning()
    assert widget.get_plc_state(7) == '未连接'
    assert widget.btn_connect.text() == '连接'
    assert not widget.services.plc_services and not widget.services.plc_threads
    widget.start_connect(row)
    new_service = widget.services.plc_services[7]
    assert new_service.service_generation != old_generation
    until(qapp, lambda: widget.get_plc_state(7) == '已连接')
    widget._on_connect_thread_stopped(7, old_thread)
    assert widget.services.plc_services[7] is new_service
    assert widget.get_plc_state(7) == '已连接'
    assert widget.btn_connect.text() == '断开'
    assert [operation for operation, owner in calls] == ['connect', 'disconnect', 'connect']
    assert all(owner != threading.get_ident() for operation, owner in calls)
    assert set(widget.state_change_threads) == {threading.get_ident()}


def test_real_settings_confirmed_thread_finish_restores_disconnected_state(connected_settings, qapp):
    from PyQt6.QtCore import QMetaObject, Qt
    widget, row, calls = connected_settings
    widget.start_connect(row)
    until(qapp, lambda: widget.get_plc_state(7) == '已连接')
    service, owner = widget.services.plc_services[7], widget.services.plc_threads[7]
    QMetaObject.invokeMethod(service, 'stop_plc_service', Qt.ConnectionType.BlockingQueuedConnection)
    owner.quit()
    assert owner.wait(3000)
    until(qapp, lambda: not widget.services.plc_threads)
    assert widget.get_plc_state(7) == '未连接'
    widget.start_connect(row)
    until(qapp, lambda: widget.get_plc_state(7) == '已连接')


def test_failed_disconnect_invocation_retains_live_owner(connected_settings, qapp, monkeypatch):
    import ui.modules.plc_settings as module
    widget, row, calls = connected_settings
    widget.start_connect(row)
    until(qapp, lambda: widget.get_plc_state(7) == '已连接')
    service, owner = widget.services.plc_services[7], widget.services.plc_threads[7]
    def fail(*args):
        raise RuntimeError('cannot dispatch stop')
    with monkeypatch.context() as patch:
        patch.setattr(module.QMetaObject, 'invokeMethod', fail)
        widget.disconnect_row(7)
        assert owner.isRunning()
        assert widget.services.plc_services[7] is service
        assert widget.services.plc_threads[7] is owner
        assert widget.get_plc_state(7) == '已连接'
    widget.start_connect(row)
    assert len(calls) == 1


def test_timed_out_thread_join_retains_owner_until_confirmed_finish(connected_settings, qapp, monkeypatch):
    import threading
    from PyQt6.QtCore import QThread
    import ui.modules.plc_settings as module
    widget, row, calls = connected_settings
    release = threading.Event()
    class SlowFinishThread(QThread):
        def run(self):
            super().run()
            release.wait(5)
    monkeypatch.setattr(module, 'QThread', SlowFinishThread)
    widget.start_connect(row)
    until(qapp, lambda: widget.get_plc_state(7) == '已连接')
    service, owner = widget.services.plc_services[7], widget.services.plc_threads[7]
    try:
        widget.disconnect_row(7)
        assert owner.isRunning()
        assert widget.services.plc_services[7] is service
        assert widget.services.plc_threads[7] is owner
        widget.start_connect(row)
        assert len(calls) == 2
        # A retry must only join: after quit the owner's event loop can no
        # longer receive another BlockingQueuedConnection stop invocation.
        invocations = []
        def unexpected_stop(*args):
            invocations.append(args)
            raise RuntimeError('event loop already stopped')
        timer = threading.Timer(.05, release.set)
        timer.start()
        try:
            with monkeypatch.context() as patch:
                patch.setattr(module.QMetaObject, 'invokeMethod', unexpected_stop)
                widget.disconnect_row(7)
            assert invocations == []
            assert not widget.services.plc_threads
        finally:
            timer.join()
    finally:
        release.set()
        assert owner.wait(3000)
        qapp.processEvents()
    until(qapp, lambda: not widget.services.plc_threads)
    assert widget.get_plc_state(7) == '未连接'
