from __future__ import annotations

import json
import struct
import time
from types import SimpleNamespace

from app.service.multi_run_coordinator_mvp.contracts import FrozenPayload
from app.service.multi_run_integration.production_composition import (
    ProductionCurrentCollectorFactory,
)
from app.service.plc_service import PlcService
import app.service.plc_service as plc_service_module
from app.services_container import (
    EventBus,
    FactoryControllerState,
    FactoryReviewPayload,
    FactoryRuntimeStartPayload,
)
from tests.test_stft_module._support import _make_stft_frame, _show
from ui.modules.stft import StftModule


class _FakeSnap7Client:
    def __init__(self, *, start_states, current_values):
        self._start_states = iter(start_states)
        self._current_values = iter(current_values)

    def db_read(self, db_number, byte_index, size):
        assert db_number == 3
        if (byte_index, size) == (0, 1):
            return bytes([1 if next(self._start_states) else 0])
        if (byte_index, size) == (4, 4):
            return struct.pack(">f", next(self._current_values))
        raise AssertionError(f"unexpected PLC read: byte={byte_index}, size={size}")


def _frozen_plc_config():
    canonical = json.dumps(
        {
            "schema": "analysis-input/v1",
            "config": {
                "config_id": 101,
                "plc_id": 7,
                "current_meter": {"enabled": True, "source": "plc"},
            },
            "channels": [],
            "channel_mapping": {
                "schema": "audio-channel-map/v1",
                "channel_order": [0],
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return SimpleNamespace(
        trigger_monotonic_ns=1_500_000_000,
        config_payload=FrozenPayload("config:101", canonical),
    )


def test_plc_start_publishes_live_current_through_collector_to_stft(
    qapp,
    monkeypatch,
):
    bus = EventBus()
    factory_mode = SimpleNamespace(
        state=FactoryControllerState(),
        review_payload=FactoryReviewPayload(),
        begin_reanalysis=lambda *_args: True,
    )
    module = StftModule(SimpleNamespace(bus=bus, factory_mode=factory_mode))
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)

    collector = None
    testing_writes = []
    current_times = iter([1.4, 1.5, 1.7])
    monkeypatch.setattr(
        plc_service_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: next(current_times),
            monotonic_ns=lambda: 1_500_000_000,
        ),
    )

    def on_start_requested(plc_id, workorder, trigger_monotonic_ns):
        nonlocal collector
        assert plc_id == 7
        assert workorder == {}
        assert trigger_monotonic_ns == 1_500_000_000
        collector = ProductionCurrentCollectorFactory(
            bus=bus,
            monotonic=time.monotonic,
        )("run-101", _frozen_plc_config())
        assert collector.start() is True
        bus.factory_state_changed.emit(
            FactoryControllerState(
                displayed_config_id=101,
                run_phase="running",
                trigger_source="plc",
            )
        )
        bus.factory_runtime_start_accepted.emit(
            FactoryRuntimeStartPayload(
                config_id=101,
                run_id="run-101",
                trigger_source="plc",
                plc_id=7,
            )
        )
        bus.stft_updated.emit(
            101,
            "run-101",
            0,
            _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
        )

    service = PlcService(
        bus,
        on_start_requested,
        "127.0.0.1",
        db_number=3,
        auto_reset_bit=False,
        field_offsets_dict={
            "start_signal_offset": "0.0",
            "current_value_offset": 4,
        },
        plc_id=7,
    )
    service.plc_client = _FakeSnap7Client(
        start_states=[False, True, True],
        current_values=[0.5, 1.0, 2.0],
    )
    service._emit_plc_workorder = lambda: {}
    service._write_testing_signal = lambda testing: testing_writes.append(testing)

    try:
        service.poll_once()
        service.poll_once()
        service.poll_once()
        qapp.processEvents()

        payload = module.canvas.heatmap.current_trace_payload()
        assert payload["times_sec"] == [0.0, 0.2]
        assert payload["values_a"] == [2.0, 2.0]
        assert payload["unit"] == "A"
        assert 0.5 not in payload["values_a"]
        assert 1.0 not in payload["values_a"]
        assert testing_writes == [True]

        unchanged = dict(payload)
        bus.plc_current_sampled.emit(8, 1.8, 99.0)
        qapp.processEvents()
        assert module.canvas.heatmap.current_trace_payload() == unchanged
    finally:
        if collector is not None:
            collector.stop()
        module.close()
        qapp.processEvents()
