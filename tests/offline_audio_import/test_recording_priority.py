"""Real ring/capture/current paths with deterministic input and AI boundaries."""
from dataclasses import replace
import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.services_container import ServiceContainer, EventBus
from app.service.offline_audio_import import ImportPhase
from app.service.run_persistence.historical_analysis_reader import HistoricalAnalysisReader
from app.service.multi_run_integration.production_composition import ProductionCurrentCollectorFactory
from app.service.analysis.resident_audio_mvp import CaptureOutcomeStatus
from tests.resident_audio_mvp.test_ring_capture import open_ready_pool
from tests.multi_run_coordinator_mvp._support import Harness, captured_request, frozen_captured, canonical_payload
from tests.offline_audio_import.test_integration import make_service
from tests.test_ai_resident_runtime import _write_fake_resident_script, _entrypoint


@pytest.mark.parametrize('import_first', [False, True])
def test_recording_admission_frames_and_current_survive_import_overlap(tmp_path, monkeypatch, qtbot, import_first):
    pool, backend, clock = open_ready_pool(monkeypatch, sample_rate_hz=100, frames_per_buffer=10)
    harness = Harness()
    bus = EventBus()
    traces = []
    bus.current_trace_updated.connect(lambda config_id, run_id, payload: traces.append((config_id, run_id, payload)))
    current_factory = ProductionCurrentCollectorFactory(bus=bus, monotonic=lambda: clock() / 1e9)
    collectors = []
    def current(run_id, frozen):
        collector = current_factory(run_id, frozen)
        collectors.append(collector)
        return collector
    harness.current_factory = current
    def freeze(request):
        frozen = frozen_captured(request, config_id='11', current_enabled=True)
        payload = json.loads(frozen.config_payload.canonical_bytes)
        payload['config'].update(config_id=11, plc_id=7, current_meter={'enabled': True, 'source': 'plc'})
        return replace(frozen, target_frame_count=75, config_payload=canonical_payload(payload),
            metadata=replace(frozen.metadata, expected_frame_count=75, expected_sample_rate_hz=100))
    harness.captured_freezer.result_factory = freeze
    capture = {}
    def start(reservation, trigger_monotonic_ns, target_frame_count):
        # The existing coordinator adapter boundary forwards into the real resident pool.
        binding = pool.reserve_capture(reservation.run_id, 'mic-1')
        capture['completion'] = pool.start_capture(binding,
            trigger_monotonic_ns=trigger_monotonic_ns, target_frame_count=target_frame_count)
        capture['binding'] = binding
    harness.resident.start = start
    coordinator = harness.coordinator()
    container = SimpleNamespace(multi_run_coordinator=coordinator)
    busy = lambda: ServiceContainer.production_busy.fget(container)
    started, release = tmp_path / 'ai-started', tmp_path / 'ai-release'
    toolchain = {'toolchain_id': 'tc-overlap', 'execution_revision': 'test',
        'execution_mode': 'resident_process', 'entrypoint': _entrypoint(_write_fake_resident_script(tmp_path)),
        'package_dir': str(tmp_path), 'timeout_seconds': 15.,
        'params': {'behavior': 'block', 'started_file': str(started), 'release_file': str(release)}}
    service, db, artifacts, request = make_service(tmp_path, toolchain=toolchain, busy=busy)
    try:
        assert not busy()
        if import_first:
            receipt = service.submit(request)
            assert receipt.accepted
            qtbot.waitUntil(started.exists, timeout=40000)
            assert service.status.phase is ImportPhase.ANALYZING
        trigger = replace(captured_request(), trigger_monotonic_ns=clock())
        diagnostic_start = time.perf_counter()
        admitted = coordinator.start_captured(trigger)
        diagnostic_admission_seconds = time.perf_counter() - diagnostic_start
        assert admitted.accepted, admitted
        assert capture['completion'].wait(0) is None
        assert trigger.run_id in coordinator.snapshot().accepted_active_run_ids
        assert busy()
        if not import_first:
            receipt = service.submit(request)
            assert receipt.accepted
            service.wait_for_phase(ImportPhase.WAITING, 5)
            assert not started.exists()
        assert collectors and collectors[0].samples() == []
        origin = clock() / 1e9
        for block in range(8):
            backend.handles['mic-1'].emit_numbered_frames(first=block * 10, count=10)
            bus.plc_current_sampled.emit(7, clock() / 1e9, block / 10.)
        outcome = capture['completion'].wait(2.)
        assert outcome.status is CaptureOutcomeStatus.COMPLETED
        assert outcome.audio.frame_count == 75
        np.testing.assert_array_equal(outcome.audio.samples[:, 0], np.arange(75, dtype=np.float32))
        samples = collectors[0].samples()
        assert [s.current_a for s in samples] == [i / 10. for i in range(8)]
        assert [s.time_sec for s in samples] == pytest.approx([.1 * (i + 1) for i in range(8)])
        assert len(traces) == 8 and all(item[:2] == (11, trigger.run_id) for item in traces)
        assert traces[-1][2]['values_a'][-8:] == [i / 10. for i in range(8)]
        assert traces[-1][2]['unit'] == 'A'
        if import_first:
            # Let the real in-flight AI decision finish while capture/save context stays busy.
            release.touch()
            service.wait_for_phase(ImportPhase.COMPLETING_CHARTS, 40)
            assert not service.status.charts_ready
        else:
            assert service.status.phase is ImportPhase.WAITING
        coordinator.stop_all(1.)
        assert not busy()
        if not import_first:
            qtbot.waitUntil(started.exists, timeout=40000)
            release.touch()
        service.wait_for_phase(ImportPhase.COMPLETED, 40)
        imported = HistoricalAnalysisReader(db, result_artifact_store=artifacts).read(receipt.run_id)
        assert imported.review['ai_pass'] is True
        assert imported.review['review']['import']['current_available'] is False
        stopped = collectors[0].samples()
        bus.plc_current_sampled.emit(7, origin + 1., 99.)
        assert collectors[0].samples() == stopped
        print(f'import_first={import_first}: admission diagnostic={diagnostic_admission_seconds:.6f}s; '
              '75 contiguous frames; 8 real collector samples')
    finally:
        release.touch()
        coordinator.stop_all(1.)
        for collector in collectors:
            collector.stop()
        assert service.close()
        pool.shutdown()
        db.close_after_upstream_stopped()
