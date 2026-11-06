"""Application-owned, serial background file/DB work and PLC config delivery.

Widgets subscribe to ``finished`` using QObject slots and retain request IDs.
Closing a widget never closes this controller. Only the application calls close.
"""
from dataclasses import dataclass, field
from threading import Event, Lock
from uuid import uuid4

from PyQt6.QtCore import Q_ARG, QMetaObject, QObject, QThread, Qt, pyqtSignal, pyqtSlot

from app.service.torque.excel import TorqueImportCancelled, parse_workbook, write_template


@dataclass(frozen=True)
class TorqueJobResult:
    request_id: str
    operation: str
    value: object = None
    error: Exception | None = None
    cancelled: bool = False
    plc_id: int | None = None


@dataclass
class _Request:
    request_id: str
    operation: str
    args: tuple
    plc_id: int | None
    cancel: Event = field(default_factory=Event)
    started: bool = False


class _Worker(QObject):
    completed = pyqtSignal(object)

    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    @pyqtSlot(object)
    def execute(self, request):
        owner = self.owner
        with owner._lock:
            request.started = True
            cancelled = request.cancel.is_set()
        value, error = None, None
        try:
            if not cancelled:
                # Peewee connections are thread-local; this never closes the UI
                # connection. Even parsing and failed validation release ours.
                with owner.store.database.connection_context():
                    if request.operation == 'parse_import':
                        value = owner.store.preview_import(parse_workbook(
                            *request.args, cancelled=request.cancel.is_set))
                    elif request.operation == 'write_template':
                        value = write_template(*request.args)
                    else:
                        value = getattr(owner.store, request.operation)(*request.args)
        except TorqueImportCancelled:
            cancelled = True
        except Exception as exc:
            error = exc
        finally:
            with owner._lock:
                owner._pending.pop(request.request_id, None)
                # Existence is validated by the store after deletion; do not
                # permanently block IDs that SQLite can reuse for a new PLC.
                if request.operation == 'delete_plc':
                    owner._deleting.discard(request.plc_id)
            self.completed.emit(TorqueJobResult(
                request.request_id, request.operation, value, error,
                cancelled, request.plc_id))

    @pyqtSlot()
    def stop(self):
        # Runs after queued requests, without any main-thread completion needed.
        QThread.currentThread().quit()


class TorqueJobs(QObject):
    finished = pyqtSignal(object)  # TorqueJobResult, delivered on controller thread
    _requested = pyqtSignal(object)
    _stop_requested = pyqtSignal()
    OPERATIONS = frozenset({
        'save_config', 'delete_plc', 'create_rule', 'upsert_rule', 'update_rule',
        'delete_rule', 'preview_import', 'parse_import', 'commit_import', 'write_template',
    })

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._lock = Lock()
        self._accepting = True
        self._pending = {}
        self._deleting = set()
        self.worker_thread = QThread(self)
        self._worker = _Worker(self)
        self._worker.moveToThread(self.worker_thread)
        self._requested.connect(self._worker.execute, Qt.ConnectionType.QueuedConnection)
        self._stop_requested.connect(self._worker.stop, Qt.ConnectionType.QueuedConnection)
        self._worker.completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self.worker_thread.finished.connect(self._worker.deleteLater)

    def submit(self, operation, *args):
        """Queue an allowed operation; return its ID without waiting for work."""
        if QThread.currentThread() != self.thread():
            raise RuntimeError('Submit torque jobs on the controller thread')
        if operation not in self.OPERATIONS:
            raise ValueError(f'Unknown torque operation: {operation}')
        plc_id = args[0].plc_id if operation == 'save_config' else (
            args[0] if operation == 'delete_plc' else None)
        request = _Request(uuid4().hex, operation, args, plc_id)
        with self._lock:
            if not self._accepting:
                raise RuntimeError('Torque jobs are closing')
            if operation == 'delete_plc':
                self._deleting.add(plc_id)
                for pending in self._pending.values():
                    if pending.operation == 'save_config' and pending.plc_id == plc_id and not pending.started:
                        pending.cancel.set()
            elif operation == 'save_config' and plc_id in self._deleting:
                request.cancel.set()
            self._pending[request.request_id] = request
        if not self.worker_thread.isRunning():
            self.worker_thread.start()
        self._requested.emit(request)
        return request.request_id

    def cancel(self, request_id):
        """Cancel pending work or an active parser; transactions finish normally."""
        with self._lock:
            request = self._pending.get(request_id)
            if request is not None:
                request.cancel.set()

    def cancel_pending_config(self, plc_id):
        """Called when deletion begins, before the existing PLC disconnect."""
        with self._lock:
            for request in self._pending.values():
                if request.operation == 'save_config' and request.plc_id == plc_id and not request.started:
                    request.cancel.set()

    @pyqtSlot(object)
    def _deliver(self, result):
        self.finished.emit(result)

    def close(self, timeout_seconds=5.0):
        """Stop acceptance, cancel pending/parser work, then join; never terminate.

        Returns None on success for ApplicationShutdownResources. A timeout raises
        so that the application keeps the database and this live owner retained.
        """
        with self._lock:
            self._accepting = False
            for request in self._pending.values():
                request.cancel.set()
        if self.worker_thread.isRunning():
            self._stop_requested.emit()
            if not self.worker_thread.wait(max(0, int(timeout_seconds * 1000))):
                raise TimeoutError('Torque background jobs did not stop')


class TorqueConfigCoordinator(QObject):
    """UI-thread bridge; persisted revisions and live generations gate receipts."""

    def __init__(self, store, jobs, bus, services, parent=None):
        super().__init__(parent)
        self.store, self.jobs, self.bus, self.services = store, jobs, bus, services
        self._receipts = {}
        jobs.finished.connect(self._job_finished)

    def bind_service(self, plc_id, service):
        service.torque_status.connect(self.bus.plc_torque_changed, Qt.ConnectionType.QueuedConnection)
        service.torque_config_applied.connect(self._config_applied, Qt.ConnectionType.QueuedConnection)

    @pyqtSlot(object)
    def _job_finished(self, result):
        if result.error is not None or result.cancelled:
            return
        if result.operation not in {'save_config', 'delete_plc'}:
            return
        config = result.value
        if (result.operation == 'save_config'
                and config.revision != self.store.get_config(config.plc_id).revision):
            return
        self.bus.plc_torque_config_saved.emit(config)
        if result.operation == 'delete_plc':
            self.bus.plc_changed.emit()
            return
        service = self.services.get(config.plc_id)
        if service is not None and self.jobs._accepting:
            QMetaObject.invokeMethod(service, 'apply_torque_config',
                                     Qt.ConnectionType.QueuedConnection, Q_ARG(object, config))

    def _receipt_is_current(self, receipt):
        service = self.services.get(receipt.plc_id)
        return (self.jobs._accepting and service is not None
                and service.service_generation == receipt.service_generation
                and self.store.get_config(receipt.plc_id).revision == receipt.config_revision)

    def get_applied_receipt(self, plc_id):
        """Return a current receipt for newly opened dialogs, or None (pending)."""
        receipt = self._receipts.get(plc_id)
        return receipt if receipt is not None and self._receipt_is_current(receipt) else None

    @pyqtSlot(object)
    def _config_applied(self, receipt):
        if self._receipt_is_current(receipt):
            self._receipts[receipt.plc_id] = receipt
            self.bus.plc_torque_config_applied.emit(receipt)
