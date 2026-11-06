import threading
import time
from types import SimpleNamespace

from app.service.offline_audio_import import ImportRequest, ImportPhase, OfflineAudioImportService


def test_submit_is_prompt_duplicate_is_rejected_and_waiting_is_cancelable():
    entered = threading.Event()
    def prepare(snapshot):
        entered.set()
        raise AssertionError('busy production must prevent preparation')
    service = OfflineAudioImportService(freeze=lambda request: request,
        prepare=prepare, runtime_factory=lambda *args: None, production_busy=lambda: True)
    started = time.monotonic()
    receipt = service.submit(ImportRequest('source.wav', 9, ()))
    assert receipt.accepted and time.monotonic() - started < .5
    assert not service.submit(ImportRequest('other.wav', 10, ())).accepted
    assert service.wait_for_phase(ImportPhase.WAITING, 2).run_id == receipt.run_id
    assert not entered.is_set()
    assert service.close(timeout_seconds=2)
    assert service.status.phase is ImportPhase.CANCELED
    assert not service.submit(ImportRequest('source.wav', 9, ())).accepted


def test_submission_freezes_before_wait_and_preparation_does_not_block_submit():
    busy = threading.Event(); busy.set()
    entered = threading.Event(); release = threading.Event()
    config = {'limit': 10}
    frozen = []
    def prepare(snapshot):
        frozen.append(snapshot)
        entered.set()
        release.wait(3)
        raise ValueError('deliberate preparation failure')
    events = []
    service = OfflineAudioImportService(freeze=lambda request: dict(config),
        prepare=prepare, runtime_factory=lambda *args: None, production_busy=busy.is_set,
        status_sink=events.append)
    try:
        receipt = service.submit(ImportRequest('source.wav', 9, ()))
        service.wait_for_phase(ImportPhase.WAITING, 2)
        config['limit'] = 99
        busy.clear()
        assert entered.wait(2)
        assert frozen == [{'limit': 10}]
        assert not service.submit(ImportRequest('source.wav', 9, ())).accepted
        release.set()
        status = service.wait_for_phase(ImportPhase.FAILED, 2)
        assert 'preparing' in status.error and 'deliberate' in status.error
        assert all(event.run_id == receipt.run_id for event in events)
        assert [event.generation for event in events] == sorted(set(event.generation for event in events))
    finally:
        release.set()
        service.close()
