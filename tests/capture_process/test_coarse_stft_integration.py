"""Real live analysis/rendering and synthetic capture isolation; no hardware."""
from copy import deepcopy
from types import SimpleNamespace
import time

import numpy as np
import pytest
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent

from app.service.analysis.stft_preview import CoarseStftFrame
from app.service.capture_process.protocol import PreviewBlock
from app.service.multi_run_integration import production_composition as composition
from app.services_container import FactoryControllerState
from tests.capture_process.test_preview import frozen
from tests.test_stft_module._support import _make_module, _show
from tests.test_stft_module.conftest import (
    _clear_pending_live_flushes,
    _close_child_plot_widgets,
    _disable_updates_and_remove_events,
)


@pytest.fixture
def stft_module(qapp):
    module, services = _make_module()
    yield module, services
    if not sip.isdeleted(module):
        _clear_pending_live_flushes(module)
        _disable_updates_and_remove_events(module)
        _close_child_plot_widgets(module)
        module.close()
    if not sip.isdeleted(module):
        _disable_updates_and_remove_events(module)
        module.deleteLater()
        QCoreApplication.sendPostedEvents(module, QEvent.Type.DeferredDelete)
    for _ in range(5):
        qapp.processEvents()
    assert sip.isdeleted(module), "Integration test leaked its StftModule"


@pytest.mark.parametrize("gap", ["sample", "generation"])
def test_production_real_pipeline_coarse_cache_widget_coverage(qapp, monkeypatch, gap, stft_module):
    source_frames, emitted, pipelines = [], [], []
    class ObservedPipeline(composition.AnalysisPipeline):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            pipelines.append(self)
        def process_chunk(self, *args, **kwargs):
            result = super().process_chunk(*args, **kwargs)
            source_frames.extend(result["stft_frames"])
            return result
    monkeypatch.setattr(composition, "AnalysisPipeline", ObservedPipeline)
    manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace(
        stft_updated=SimpleNamespace(emit=lambda *args: emitted.append(args))))
    rate = 44100
    value = frozen(rate=rate, frames=rate * 10)
    value.config_payload = composition._payload("config", {"config": {
        "config_id": 101, "selected_channels": [0], "channels": [{"channel_index": 0,
        "stft_nfft": 1024, "stft_overlap": .5, "fft_nfft": 1024}]}})
    original_payload = deepcopy(value.config_payload)
    pcm = np.sin(2 * np.pi * 997 * np.arange(rate * 3) / rate).astype(np.float32)[:, None]
    original_pcm = pcm.copy()
    try:
        manager.register_frozen(value)
        assert manager.wait_until_setup_idle(2)
        run = manager._runs_by_hardware["managed-input-1"][value.run_id]
        manager.accept_preview(PreviewBlock("a" * 32, "managed-input-1", "g1", 0, 0,
            value.trigger_monotonic_ns, rate, pcm))
        assert manager.wait_until_idle(3)
        assert run.dispatcher.wait_until_idle(3)
        assert emitted and all(isinstance(args[-1], CoarseStftFrame) for args in emitted)
        coarse = [args[-1] for args in emitted]
        assert sum(frame.source_frame_count for frame in coarse) == len(source_frames)
        assert all(len(frame.frequencies) <= 256 for frame in coarse)
        assert 25 <= len(coarse) <= 35
        hop = 512 / rate
        assert np.median([end - start for start, end in (f.time_bounds for f in coarse)]) == pytest.approx(.1, abs=hop)
        np.testing.assert_allclose([f.time_bounds[0] for f in coarse[1:]],
                                   [f.time_bounds[1] for f in coarse[:-1]], atol=1e-12)
        module, services = stft_module
        module.selected_display_channel_provider = lambda: 0
        _show(module, qapp)
        services.bus.factory_state_changed.emit(FactoryControllerState(displayed_config_id=101, run_phase="running"))
        for args in emitted:
            services.bus.stft_updated.emit(*args)
        module.flush_pending_live_refresh()
        cache = module._stft_cache[(101, value.run_id, 0)]
        assert len(cache.times_sec) == len(coarse)
        assert np.isfinite(module.canvas.heatmap._image_item.image).all()
        assert module.canvas.heatmap.rendered_time_extent_sec() == pytest.approx(
            (source_frames[0].time - hop / 2, source_frames[-1].time + hop / 2))
        count = len(emitted)
        manager.accept_preview(PreviewBlock("a" * 32, "managed-input-1",
            "g2" if gap == "generation" else "g1", 1,
            0 if gap == "generation" else rate * 4,
            value.trigger_monotonic_ns + 4_000_000_000, rate, pcm[:rate]))
        assert manager.wait_until_idle(3)
        assert run.dispatcher.wait_until_idle(3)
        for args in emitted[count:]:
            services.bus.stft_updated.emit(*args)
        module.flush_pending_live_refresh()
        assert sum(args[-1].source_frame_count for args in emitted) == len(source_frames)
        assert len(cache.times_sec) == len(emitted)
        widget = module.canvas.heatmap
        raster = widget._image_item.image
        left, right = widget.rendered_time_extent_sec()
        centers = left + (np.arange(raster.shape[1]) + .5) * (right - left) / raster.shape[1]
        gap_pixels = (centers > 3.05) & (centers < 3.95)
        assert gap_pixels.any() and np.isnan(raster[:, gap_pixels]).all()
        assert pipelines[0]._nfft == 1024 and pipelines[0]._overlap == .5
        assert value.config_payload == original_payload
        np.testing.assert_array_equal(pcm, original_pcm)
    finally:
        assert manager.close(3)


def test_legacy_test_session_enables_coarse_preview_without_config_changes(tmp_path):
    from app.service.analysis.test_session import TestSession
    from tests.test_test_session import _make_config, _make_mock_bus
    config = _make_config()
    snapshot = deepcopy(config)
    session = TestSession(config, _make_mock_bus(), None, {}, {}, str(tmp_path))
    try:
        session._setup_pipelines()
        assert session._live_display_dispatcher._coarse_stft is True
        assert session._live_display_dispatcher._coarse_interval_sec == .05
        assert session._pipelines[0]._nfft == config["channels"][0]["stft_nfft"]
        assert config == snapshot
    finally:
        session.stop_live_display()


@pytest.mark.parametrize("hold_preview_lock", [False, True])
def test_synthetic_capture_preserves_exact_pcm_with_stalled_preview(hold_preview_lock):
    from tests.capture_process.test_worker import make_adapter
    from tests.capture_process.fake_backend import endpoint
    from app.service.analysis.resident_audio_mvp import CaptureOutcomeStatus
    adapter = make_adapter()
    lock = None
    try:
        ready = adapter.open_startup_streams((endpoint(),)).ready_streams[0]
        hardware = f"managed-input-{ready.endpoint.hardware_id.value}"
        deadline = time.monotonic() + 2
        blocks = []
        while not blocks and time.monotonic() < deadline:
            blocks = adapter.client.preview.read(hardware)
            if not blocks:
                time.sleep(.01)
        assert blocks
        first = blocks[-1]
        if hold_preview_lock:
            lock = adapter.client.preview.bootstrap.streams[0].lock
            assert lock.acquire(timeout=1)
        route = adapter.capture_target(ready.endpoint.hardware_id, ready.generation)
        binding = route.pool.reserve_capture("coarse-preview-stall", hardware)
        outcome = route.pool.start_capture(binding, trigger_monotonic_ns=first.monotonic_ns,
                                           target_frame_count=400).wait(3)
        assert outcome.status is CaptureOutcomeStatus.COMPLETED
        expected = np.arange(first.first_sample, first.first_sample + 400, dtype=np.float32)
        assert outcome.audio.samples.shape == (400, 2)
        np.testing.assert_array_equal(outcome.audio.samples, np.column_stack((expected, expected + 1000)))
        assert not outcome.audio.samples.flags.writeable
    finally:
        if lock is not None:
            lock.release()
        adapter.close_all(2.0)
    assert not adapter.client.process.is_alive()
