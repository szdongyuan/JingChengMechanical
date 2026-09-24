"""Real timing -> Resident -> coordinator -> WAV -> scheduler/AI regressions.

Only native device lifecycle and external judgment/AI computation are faked.
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4
import wave

import numpy as np
import pytest

from app.service.analysis.production_executor import ProductionAnalysisExecutor
from app.service.analysis.resident_audio_mvp import BackendCallbackStatus, BackendCallbackDirective
from app.service.current_meter_service import PlcCurrentCollector
from app.service.plc_current_history import PlcCurrentHistory
from app.services_container import EventBus
from app.service.managed_input_sounddevice_backend import MonotonicSoundDeviceAudioBackend
from app.service.multi_run_coordinator_mvp import (
    ManagedResidentCapturePort, ManagedResidentCaptureRoute, MultiRunCoordinator,
    CapturedStartRequest, TriggerSource, CommitResult, TerminalErrorCategory,
)
from app.service.plc_result_sequencer import PlcResultSequencer
from app.service.run_persistence import (
    RunPersistenceDatabase, ProductionRunAudioAdapter, AudioPersistenceService,
    AudioPublication, AudioRepository, RunRepository, settle_startup,
)
from app.service.run_persistence.prepare_database import prepare_run_persistence_database
from tests.test_managed_input_resident_adapter import make_adapter, endpoint
from tests.test_managed_input_sounddevice_backend import _SoundDevice
from tests.test_managed_input_timeline_integrity import Clock
from tests.resident_audio_mvp.fakes import FakeBackend, ManualMonotonicClock
from tests.resident_audio_mvp.test_recovery import StaticResolver
from tests.multi_run_coordinator_mvp._support import (
    FakeDiagnostics, FakeHistoryProjector, FakeHistorySink,
    canonical_payload, payload, run_config_payload,
)
from tests.multi_run_coordinator_mvp.test_composite_end_to_end import (
    _CapturedFreezer, _ImportedFreezer, _CoordinatorPersistence,
    _SynchronousProductionSaveDispatcher,
)
from tests.run_persistence.test_scheduler_bridge_end_to_end import _build_real_stack, ACCEPTED_AT
from tests.analysis_scheduler.test_production_executor import _Judgment, _ResidentAiRuntime, _toolchain

RATE = 48_000
BLOCK = 480
FRAMES = RATE * 8


class TimedBackend(FakeBackend):
    """Fake lifecycle around the actual production timing callback."""
    def __init__(self):
        super().__init__(clock=ManualMonotonicClock(2_000_000_000))
        self.authority = Clock()
        self.timing = {}
        self.adc_end = {}

    def create_input(self, spec, callback, on_fault):
        timing = MonotonicSoundDeviceAudioBackend(_SoundDevice(), self.authority).create_input(
            spec, callback, on_fault)
        self.timing[spec.hardware_id] = timing
        self.adc_end[spec.hardware_id] = self.clock()

        def raw(samples, frames, timestamp, status):
            end = self.adc_end[spec.hardware_id]
            self.authority.now_ns = self.clock()
            return timing._native_callback(samples, frames, SimpleNamespace(
                currentTime=self.clock() / 1e9,
                inputBufferAdcTime=end / 1e9 - frames / RATE), status)
        return super().create_input(spec, raw, on_fault)

    def emit_block(self, hardware, samples, *, delay_ns=0, gap_ns=0, status=BackendCallbackStatus.OK):
        self.adc_end[hardware] += round(len(samples) * 1e9 / RATE) + gap_ns
        self.clock._now_ns = max(self.clock(), self.adc_end[hardware] + delay_ns)
        return self.handles[hardware].emit(samples, status=status)


class FourSegmentFreezer(_CapturedFreezer):
    def freeze(self, request):
        base = super().freeze(request)
        envelope = json.loads(base.analysis_input.canonical_bytes)
        envelope['channel_mapping']['channel_order'] = [0]
        envelope['config'].update(selected_channels=[0], input_device_samplerate=RATE,
                                  channels=[{'channel_index': 0, 'spl_weighting': 'Z'}])
        toolchain = asdict(_toolchain(execution_mode='resident_process'))
        toolchain.pop('runtime_lane_key')
        toolchain['params'] = json.loads(toolchain.pop('params_json'))
        envelope['analysis'].update(window_start_seconds=0.25, window_end_seconds=7.75,
                                    toolchain=toolchain)
        return replace(base, target_frame_count=FRAMES,
                       metadata=replace(base.metadata, expected_frame_count=FRAMES, expected_channel_count=1),
                       config_payload=run_config_payload(current_enabled=True),
                       analysis_input=canonical_payload(envelope))


def pcm(path):
    with wave.open(str(path), 'rb') as reader:
        assert (reader.getframerate(), reader.getnchannels(), reader.getnframes()) == (RATE, 1, FRAMES)
        return np.frombuffer(reader.readframes(FRAMES), dtype='<i2').copy()


def four_segments():
    # Each stage has its own amplitude band; every within-stage frame is numbered.
    return np.concatenate([(np.arange(RATE * 2) % 4096 + stage * 6000).astype(np.float32) / 32768
                           for stage in range(1, 5)]).reshape(-1, 1)


class RecordingExecutor:
    def __init__(self):
        self.calls = []
        self.results = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.condition = threading.Condition()
        class Judgment(_Judgment):
            def rejudge(self, *args, **kwargs):
                result = super().rejudge(*args, **kwargs)
                result.analysis_window = (args[5], args[6])
                return result
        self.judgment = Judgment()
        self.ai = _ResidentAiRuntime(self.ai_result)
        self.delegate = ProductionAnalysisExecutor(judgment_engine=self.judgment, ai_runtime=self.ai)

    def ai_result(self, kwargs):
        return {'module_status': 'success', 'response_payload': {
            'protocol_version': 1, 'request_id': kwargs['request_id'],
            'ai_result': {'ai_label': 'PASS', 'score': 0.9}}}

    def execute(self, job, audio, token):
        with self.condition:
            self.calls.append((job, audio.samples.copy()))
            self.condition.notify_all()
        if len(self.calls) == 1:
            self.entered.set()
            assert self.release.wait(10), 'right analysis barrier not released'
        try:
            result = self.delegate.execute(job, audio, token)
        except Exception as exc:
            self.results.append(__import__("traceback").format_exc())
            raise
        self.results.append(result)
        return result


@pytest.fixture
def stack(tmp_path):
    backend = TimedBackend()
    class Resolver(StaticResolver):
        def __init__(self):
            super().__init__()
            self.release = threading.Event()
            self.release.set()
        def resolve(self, frozen):
            assert self.release.wait(5)
            return super().resolve(frozen)
    resolver = Resolver()
    drained = threading.Condition()
    drain_times = {}
    def observe(hardware, samples, frames, end, rate):
        with drained:
            drain_times[hardware] = end
            drained.notify_all()
    adapter = make_adapter(backend, frames_per_buffer=BLOCK, max_capture_frames=FRAMES,
                           drain_observer=observe, endpoint_resolver=resolver)
    startup = adapter.open_startup_streams((endpoint(1, input_channels=1), endpoint(2, input_channels=1)))
    assert len(startup.ready_streams) == 2
    routes = tuple(ManagedResidentCaptureRoute(f'hardware-{item.endpoint.hardware_id.value}',
                    item.endpoint.hardware_id, item.generation) for item in startup.ready_streams)
    resident = ManagedResidentCapturePort(adapter, routes)
    runtime = RunPersistenceDatabase(prepare_run_persistence_database(tmp_path / 'runs.sqlite3'))
    assert settle_startup(runtime, ACCEPTED_AT).ready
    audio_adapter = ProductionRunAudioAdapter(AudioPersistenceService(
        AudioPublication(), AudioRepository(runtime), RunRepository(runtime)))
    executor = RecordingExecutor()
    bridge, _ = _build_real_stack(runtime, audio_adapter, executor=executor,
                                  now_utc=lambda: '2026-07-27T12:01:00.000000Z')
    paths = {}
    condition = threading.Condition()
    terminals = {}
    trace = []
    class History(FakeHistorySink):
        def publish(self, event):
            super().publish(event)
            if event.projection.phase.name == 'TERMINAL':
                with condition:
                    terminals[event.run_id] = event
                    condition.notify_all()
    class Persistence(_CoordinatorPersistence):
        def __init__(self, runtime):
            super().__init__(runtime)
            self.failed = []
        def commit_initial_terminal(self, fact):
            self.failed.append(fact)
            return CommitResult.COMMITTED
    persistence = Persistence(runtime)
    saves = _SynchronousProductionSaveDispatcher(audio_adapter)
    bus = EventBus()
    currents = PlcCurrentHistory(bus=bus, plc_ids=(1, 2), monotonic=lambda: backend.clock() / 1e9)
    collectors = {}
    def current_factory(run_id, frozen):
        collector = PlcCurrentCollector({'enabled': True, 'source': 'plc'}, bus=bus,
            plc_id=int(frozen.hardware_id[-1]), history=currents,
            time_origin=frozen.trigger_monotonic_ns / 1e9)
        collectors[run_id] = collector
        return collector
    coordinator = MultiRunCoordinator(
        captured_freezer=FourSegmentFreezer(paths),
        imported_freezer=_ImportedFreezer(tmp_path / 'unused.wav', tmp_path / 'unused-final.wav'),
        resident=resident, persistence=persistence, save_dispatcher=saves, scheduler=bridge,
        history_projector=FakeHistoryProjector(trace), history_sink=History(trace),
        plc=PlcResultSequencer(write_result=lambda *_: None, report_attempt=lambda _: None),
        diagnostics=FakeDiagnostics(), current_collector_factory=current_factory,
        save_job_id_factory=lambda: str(uuid4()),
        wall_clock=lambda: datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
        monotonic_clock=time.monotonic,
    )
    def start(station, trigger=None, *, auto_continue=True):
        run_id = str(uuid4())
        paths[run_id] = (f'hardware-{station}', tmp_path / f'{run_id}.wav')
        result = coordinator.start_captured(CapturedStartRequest(
            run_id=run_id, trigger_monotonic_ns=backend.adc_end[f'managed-input-{station}'] if trigger is None else trigger,
            source=TriggerSource.MANUAL, plc_id=None, ingress_payload=payload('capture'), fallback_history_labels=payload('fallback')))
        if auto_continue and callable(getattr(result, 'wait', None)):
            result.wait()
            result = result.continuation()
        return run_id, result
    state = SimpleNamespace(**locals())
    try:
        yield state
    finally:
        executor.release.set()
        resolver.release.set()
        coordinator.begin_shutdown()
        bridge.stop_and_join_scheduler(5.0)
        coordinator.finish_shutdown(time.monotonic() + 5)
        resident.close_and_join(5)
        adapter.close_all(5.0)
        currents.close()
        bridge.close_persistence_after_upstream_stopped()


def feed(stack, station, samples):
    for index in range(0, len(samples), BLOCK):
        # 300 ms delayed callbacks followed by delivery bursts; ADC stays contiguous.
        assert stack.backend.emit_block(f'managed-input-{station}', samples[index:index + BLOCK],
            delay_ns=300_000_000 if index % (BLOCK * 30) == 0 else 0) is BackendCallbackDirective.CONTINUE
        if index % (BLOCK * 30) == BLOCK * 29 or index + BLOCK >= len(samples):
            end = stack.backend.timing[f'managed-input-{station}']._last_callback_audio_end_ns
            with stack.drained:
                assert stack.drained.wait_for(
                    lambda: stack.drain_times.get(f'managed-input-{station}', 0) >= end, 5)


def test_blocked_right_analysis_preserves_left_four_segments_wav_ai_and_current_origin(stack, caplog):
    caplog.set_level('INFO')
    samples = four_segments()
    right, result = stack.start(1)
    assert result.accepted
    feed(stack, 1, samples)
    assert stack.executor.entered.wait(5)
    # Keep the other continuously open input current before the left trigger.
    feed(stack, 2, np.zeros_like(samples))
    trigger = stack.backend.adc_end['managed-input-2']
    stack.bus.plc_current_sampled.emit(2, trigger / 1e9, 1.0)
    stack.bus.plc_current_sampled.emit(1, trigger / 1e9 + 0.1, 99.0)
    stack.bus.plc_current_sampled.emit(2, trigger / 1e9 + 0.2, 2.0)
    # Run preparation is delayed, but capture/current must start at observation.
    stack.backend.clock.advance_ns(300_000_000)
    left, result = stack.start(2, trigger)
    assert result.accepted
    assert stack.persistence.active_calls[-1].trigger_monotonic_ns == trigger
    feed(stack, 2, samples)
    # Save is independent of the blocked scheduler worker.
    with stack.condition:
        pass
    deadline = time.monotonic() + 5
    while not stack.paths[left][1].exists() and time.monotonic() < deadline:
        threading.Event().wait(0.005)
    assert stack.paths[left][1].exists()
    assert len(stack.executor.calls) == 1
    assert not stack.executor.release.is_set()
    expected = np.floor(samples[:, 0].astype(np.float64) * 32767 + 0.5).astype(np.int16)
    np.testing.assert_array_equal(pcm(stack.paths[left][1]), expected)
    saved = next(call for call in stack.saves.calls if call.run_id == left)
    np.testing.assert_array_equal(np.frombuffer(saved.audio.interleaved_bytes, '<f4').reshape(-1, 1), samples)
    stack.executor.release.set()
    with stack.condition:
        assert stack.condition.wait_for(lambda: right in stack.terminals and left in stack.terminals, 10)
    assert len(stack.executor.calls) == 2
    assert len(stack.executor.ai.calls) == 2, "\n".join(str(x) for x in stack.executor.results)
    job, loaded = stack.executor.calls[1]
    assert job.audio.absolute_path == str(stack.paths[left][1].resolve())
    assert job.audio.frame_count == FRAMES
    np.testing.assert_array_equal(loaded[:, 0], expected)
    request = stack.executor.ai.calls[1]['request_payload']
    assert request['segments'][0]['audio_path'] == job.audio.absolute_path
    assert request['window_info']['final_window_offset_ms'] == 250
    assert request['window_info']['final_window_duration_ms'] == 7500
    np.testing.assert_array_equal(pcm(request['segments'][0]['audio_path']), expected)
    current = stack.collectors[left].samples()
    assert [(s.time_sec, s.current_a) for s in current] == [(0.0, 1.0), (pytest.approx(0.2), 2.0)]
    mapped = [record for record in caplog.records if record.msg.startswith('capture mapped')]
    assert len(mapped) == 2
    assert f'run={left}' in mapped[1].getMessage()
    assert f'trigger_ns={trigger}' in mapped[1].getMessage()
    assert f'frames={FRAMES}' in mapped[1].getMessage()
    assert 'hardware=managed-input-2' in mapped[1].getMessage()
    assert len(mapped[1].getMessage()) < 512


@pytest.mark.parametrize('fault', ['overflow', 'adc_gap'])
def test_capture_fault_prevents_publication_and_analysis_then_recovers(stack, fault):
    run_id, result = stack.start(1)
    assert result.accepted
    block = four_segments()[:BLOCK]
    assert stack.backend.emit_block('managed-input-1', block) is BackendCallbackDirective.CONTINUE
    stack.resolver.release.clear()
    directive = stack.backend.emit_block('managed-input-1', block,
        gap_ns=500_000_000 if fault == 'adc_gap' else 0,
        status=BackendCallbackStatus.INPUT_OVERFLOW if fault == 'overflow' else BackendCallbackStatus.OK)
    assert directive is BackendCallbackDirective.STOP
    with stack.condition:
        assert stack.condition.wait_for(lambda: run_id in stack.terminals, 5)
    assert stack.persistence.failed[-1].error_category is TerminalErrorCategory.CAPTURE_FAILED
    assert stack.saves.calls == []
    assert not stack.paths[run_id][1].exists()
    assert stack.executor.calls == []
    assert stack.executor.ai.calls == []
    # Existing readiness replaces the failed generation and rejects that trigger.
    rejected_id, parked = stack.start(1, stack.backend.clock(), auto_continue=False)
    assert callable(parked.wait)
    stack.resolver.release.set()
    parked.wait()
    rejected = parked.continuation()
    assert not rejected.accepted or rejected.terminal is not None
    assert stack.executor.calls == []
    later, accepted = stack.start(1, stack.backend.clock())
    assert accepted.accepted
    stack.executor.release.set()
    feed(stack, 1, four_segments())
    with stack.condition:
        assert stack.condition.wait_for(lambda: later in stack.terminals, 10)
    assert len(stack.executor.calls) == 1
    assert len(stack.executor.ai.calls) == 1
    assert stack.paths[later][1].exists()
