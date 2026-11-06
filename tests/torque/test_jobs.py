import threading
import time

import pytest
from peewee import SqliteDatabase
from PyQt6.QtCore import QObject, pyqtSlot

from app.service.torque.jobs import TorqueJobs
from app.service.torque.migration import apply_torque_migration
from app.service.torque.store import TorqueStore
from app.service.torque.types import TorqueConfig, TorqueImportRow


def until(qapp, predicate):
    deadline = time.monotonic() + 4
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert predicate()


@pytest.fixture
def job_env(tmp_path, qapp):
    db = SqliteDatabase(tmp_path / 'jobs.db')
    db.connect()
    apply_torque_migration(db)
    db.execute_sql('CREATE TABLE plc_table (id INTEGER PRIMARY KEY)')
    db.execute_sql('CREATE TABLE plc_field_offset_table (plc_id INTEGER PRIMARY KEY)')
    db.execute_sql('INSERT INTO plc_table VALUES (7)')
    store = TorqueStore(db)
    jobs = TorqueJobs(store)
    results = []
    jobs.finished.connect(results.append)
    yield jobs, store, db, results
    jobs.close()
    db.close()


def test_jobs_are_lazy_save_off_ui_and_close_only_worker_connection(job_env, qapp, monkeypatch):
    jobs, store, db, results = job_env
    assert not jobs.worker_thread.isRunning()
    main_connection = db.connection()
    threads, closed = [], []
    save, close = store.save_config, db.close
    def saving(*args):
        threads.append(threading.get_ident())
        return save(*args)
    def closing():
        closed.append(threading.get_ident())
        return close()
    monkeypatch.setattr(store, 'save_config', saving)
    monkeypatch.setattr(db, 'close', closing)
    request = jobs.submit('save_config', TorqueConfig(7), 1)
    until(qapp, lambda: len(results) == 1)
    assert results[0].request_id == request
    assert results[0].value.revision == 1
    assert results[0].error is None
    assert threads[0] != threading.get_ident()
    assert closed == threads
    assert db.connection() is main_connection


def test_real_excel_preview_and_conflicting_commits(job_env, qapp, tmp_path, monkeypatch):
    from app.service.torque import jobs as module
    from app.service.torque.excel import write_template
    jobs, store, db, results = job_env
    path = tmp_path / 'template.xlsx'
    write_template(path)
    threads = []
    parse = module.parse_workbook
    def parsing(*args, **kwargs):
        threads.append(threading.get_ident())
        return parse(*args, **kwargs)
    monkeypatch.setattr(module, 'parse_workbook', parsing)
    jobs.submit('parse_import', path)
    until(qapp, lambda: len(results) == 1)
    preview = results[0].value
    assert preview.additions and not store.snapshot().values
    assert threads[0] != threading.get_ident()
    jobs.submit('commit_import', preview)
    jobs.submit('commit_import', preview)
    until(qapp, lambda: len(results) == 3)
    assert results[1].error is None
    assert '变化' in str(results[2].error)


def test_error_closes_connection_and_destroyed_receiver_does_not_own_job(job_env, qapp):
    jobs, store, db, results = job_env
    class Window(QObject):
        @pyqtSlot(object)
        def done(self, result):
            raise AssertionError('deleted receiver called')
    window = Window()
    jobs.finished.connect(window.done)
    from PyQt6 import sip
    sip.delete(window)
    jobs.submit('create_rule', 'sn', '', 2)
    jobs.submit('create_rule', 'sn', '000123', 2)
    until(qapp, lambda: len(results) == 2)
    assert results[0].error is not None
    assert results[1].error is None
    assert store.snapshot().values[('sn', '000123')] == 2


def test_shutdown_completes_entered_transaction_cancels_pending_without_ui_pump(job_env):
    jobs, store, db, results = job_env
    entered, release = threading.Event(), threading.Event()
    def pause(value):
        entered.set()
        assert release.wait(3)
        return value
    db.register_function(pause, 'pause_write', 1)
    db.execute_sql('CREATE TRIGGER wait_write BEFORE INSERT ON torque_rule_table BEGIN SELECT pause_write(NEW.torque_value); END')
    jobs.submit('create_rule', 'model', 'M100', 2)
    assert entered.wait(3)
    jobs.submit('create_rule', 'model', 'cancelled', 3)
    timer = threading.Timer(.05, release.set)
    timer.start()
    try:
        jobs.close()
    finally:
        release.set()
        timer.join()
    assert not jobs.worker_thread.isRunning()
    assert dict(store.snapshot().values) == {('model', 'M100'): 2}
    with pytest.raises(RuntimeError):
        jobs.submit('create_rule', 'model', 'late', 4)


def test_delete_cancels_queued_config_and_prevents_recreation(job_env, qapp, monkeypatch):
    jobs, store, db, results = job_env
    entered, release = threading.Event(), threading.Event()
    original = store.create_rule
    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(store, 'create_rule', blocked)
    jobs.submit('create_rule', 'sn', 'keep', 2)
    assert entered.wait(3)
    pending = jobs.submit('save_config', TorqueConfig(7), 1)
    deletion = jobs.submit('delete_plc', 7)
    release.set()
    until(qapp, lambda: len(results) == 3)
    assert next(r for r in results if r.request_id == pending).cancelled
    assert next(r for r in results if r.request_id == deletion).error is None
    assert not db.execute_sql('SELECT * FROM plc_table').fetchall()
    assert not db.execute_sql('SELECT * FROM plc_torque_config_table').fetchall()
    assert not store.get_config(7).enabled
    jobs.submit('save_config', TorqueConfig(7), 1)
    until(qapp, lambda: len(results) == 4)
    assert results[-1].error is not None
    # SQLite may reuse the highest deleted integer primary key for a new PLC.
    db.execute_sql('INSERT INTO plc_table VALUES (7)')
    jobs.submit('save_config', TorqueConfig(7), 1)
    until(qapp, lambda: len(results) == 5)
    assert results[-1].error is None


def test_shutdown_requests_parser_cancellation(job_env, monkeypatch):
    from app.service.torque import jobs as module
    from app.service.torque.excel import TorqueImportCancelled
    jobs, store, db, results = job_env
    entered = threading.Event()
    def parse(path, cancelled):
        entered.set()
        deadline = time.monotonic() + 3
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(.002)
        assert cancelled()
        raise TorqueImportCancelled()
    monkeypatch.setattr(module, 'parse_workbook', parse)
    jobs.submit('parse_import', 'unused.xlsx')
    assert entered.wait(3)
    jobs.close()
    assert not jobs.worker_thread.isRunning()


def test_timeout_retains_live_worker_until_transaction_finishes(job_env, monkeypatch):
    jobs, store, db, results = job_env
    entered, release = threading.Event(), threading.Event()
    original = store.create_rule
    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(store, 'create_rule', blocked)
    jobs.submit('create_rule', 'model', 'M100', 2)
    assert entered.wait(3)
    try:
        with pytest.raises(TimeoutError):
            jobs.close(timeout_seconds=.01)
        assert jobs.worker_thread.isRunning()
        assert not db.is_closed()
    finally:
        release.set()
        jobs.close()
    assert store.snapshot().values[('model', 'M100')] == 2


def test_failed_job_closes_connection_and_results_arrive_on_ui_thread(job_env, qapp, monkeypatch):
    jobs, store, db, results = job_env
    closed, received = [], []
    original = db.close
    def close():
        result = original()
        closed.append((threading.get_ident(), db.is_closed()))
        return result
    monkeypatch.setattr(db, 'close', close)
    class Receiver(QObject):
        @pyqtSlot(object)
        def done(self, result):
            received.append(threading.get_ident())
    receiver = Receiver()
    jobs.finished.connect(receiver.done)
    jobs.submit('create_rule', 'sn', '', 2)
    until(qapp, lambda: received)
    assert results[0].error is not None
    assert closed == [(closed[0][0], True)]
    assert closed[0][0] != threading.get_ident()
    assert received == [threading.get_ident()]
    assert not db.is_closed()
