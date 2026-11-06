from types import SimpleNamespace
import pytest

from app.services_container import ServiceContainer
from app.service.multi_run_coordinator_mvp import TriggerSource


def test_start_diagnostic_distinguishes_observation_queue_and_worker(monkeypatch):
    now = [2_000_000_000]
    monkeypatch.setattr('app.services_container.time.monotonic_ns', lambda: now[0])
    work, logs, requests = [], [], []
    def submit(callback, *, lane_key):
        work.append(callback)
        assert lane_key == ('plc', 7)
        return True
    services = SimpleNamespace(
        ingress_open=True,
        _prepared_config_routes=((101, 7),),
        _captured_start_dispatcher=SimpleNamespace(try_submit=submit),
        multi_run_coordinator=SimpleNamespace(start_captured=requests.append),
        build_captured_start_request=lambda **kwargs: SimpleNamespace(run_id='run-1', **kwargs),
        bus=SimpleNamespace(log=SimpleNamespace(emit=logs.append)),
    )
    assert ServiceContainer.submit_captured_start(services, source=TriggerSource.PLC,
        trigger_monotonic_ns=1_900_000_000, workorder={}, plc_id=7, config_id=101)
    now[0] = 2_500_000_000
    work[0]()
    assert requests[0].trigger_monotonic_ns == 1_900_000_000
    assert len(logs) == 1
    assert all(value in logs[0] for value in (
        'run=run-1', 'plc=7', 'config=101', 'trigger_ns=1900000000',
        'queued_ns=2000000000', 'worker_ns=2500000000'))
    assert len(logs[0]) < 512


@pytest.mark.parametrize('built_request', [object(), SimpleNamespace(run_id='run-1')])
def test_diagnostic_sink_failure_does_not_prevent_capture(built_request):
    calls = []
    def bad_log(message):
        raise RuntimeError('diagnostic sink unavailable')
    services = SimpleNamespace(
        ingress_open=True, _prepared_config_routes=((101, 7),),
        _captured_start_dispatcher=SimpleNamespace(try_submit=lambda work, **_: work() or True),
        multi_run_coordinator=SimpleNamespace(start_captured=calls.append),
        build_captured_start_request=lambda **_: built_request,
        bus=SimpleNamespace(log=SimpleNamespace(emit=bad_log)),
    )
    assert ServiceContainer.submit_captured_start(services, source=TriggerSource.PLC,
        trigger_monotonic_ns=1_900_000_000, workorder={}, plc_id=7, config_id=101)
    assert calls == [built_request]
