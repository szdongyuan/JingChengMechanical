from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.session_capture import CapturedAudioBlock
from app.service.analysis.test_manager import TestManager

from ._support import (
    _CaptureRecorderDoubleMixin,
    _DummySignal,
    _FakeSession,
    _factory_runtime_config,
    _mock_bus,
    _mock_services,
)


def _start_config(**overrides):
    config = _factory_runtime_config()
    config["output_device_name"] = None
    config.update(overrides)
    return config


def _install_start_boundary_doubles(monkeypatch):
    class BoundaryRecorder(_CaptureRecorderDoubleMixin):
        instances = []

        def __init__(
            self,
            *,
            device_name,
            hostapi,
            sample_rate,
            n_channels,
            record_duration,
        ):
            self._sample_rate = float(sample_rate)
            self._n_channels = int(n_channels)
            self._buffer = np.zeros(
                (int(self._sample_rate * record_duration), self._n_channels),
                dtype=np.float32,
            )
            self.error_occurred = _DummySignal()
            self._running = False
            self.start_calls = 0
            self.stop_calls = 0
            BoundaryRecorder.instances.append(self)

        def isRunning(self):
            return self._running

        def start(self):
            self.start_calls += 1
            self._running = True

        def stop(self):
            self.stop_calls += 1
            self._running = False

        def wait(self, timeout):
            return True

    monkeypatch.setattr(
        "app.service.analysis.test_manager.AudioRecorder",
        BoundaryRecorder,
    )
    monkeypatch.setattr(
        "app.service.analysis.test_manager.TestSession",
        _FakeSession,
    )
    return BoundaryRecorder


class TestTestManagerStartStop:
    def test_start_with_config_id_creates_session(self):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = {
                "config_id": 1,
                "config_name": "Test",
                "record_duration": 2.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "fft_nfft": 4096,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            }
            mgr = TestManager(bus, services)
            accepted = mgr.start({"config_id": 1, "sn": "SN1"})
            assert accepted is True
            assert mgr.is_running(config_id=1) is True
            bus.test_started.emit.assert_called_with(1)
            mgr.stop(config_id=1)

    def test_start_missing_workorder_config_id_sets_visible_start_error(self):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.list_configs.return_value = []
            mgr = TestManager(bus, services)

            accepted = mgr.start({})

            assert accepted is False
            assert mgr.last_start_error() == "无法解析启动配置，请重新选择配置。"
            bus.test_started.emit.assert_not_called()

    def test_start_config_load_failure_sets_visible_start_error(self):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.side_effect = RuntimeError(
                "config db unavailable"
            )
            mgr = TestManager(bus, services)

            accepted = mgr.start({"config_id": 1})

            assert accepted is False
            assert mgr.last_start_error() == "配置加载失败：config db unavailable"
            bus.test_started.emit.assert_not_called()

    def test_start_missing_saved_input_hardware_sets_visible_start_error(self):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = _start_config()
            services.hss_service.load_io_devices.return_value = {}
            mgr = TestManager(bus, services)
            accepted_callbacks = []

            accepted = mgr.start({"config_id": 1}, on_start_accepted=accepted_callbacks.append)

            assert accepted is False
            assert accepted_callbacks == []
            assert mgr.last_start_error() == "未配置输入硬件，请先在「硬件」页面保存输入设备。"
            bus.test_started.emit.assert_not_called()

    def test_start_hardware_load_failure_sets_visible_start_error(self):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = _start_config()
            services.hss_service.load_io_devices.side_effect = RuntimeError(
                "device db unavailable"
            )
            mgr = TestManager(bus, services)

            accepted = mgr.start({"config_id": 1})

            assert accepted is False
            assert mgr.last_start_error() == "读取硬件配置失败：device db unavailable"
            bus.test_started.emit.assert_not_called()

    @pytest.mark.parametrize(
            "input_payload",
            [
                {"name": "Mic", "type": "MME", "samplerate": "0", "channel_count": "1"},
                {"name": "Mic", "type": "MME", "samplerate": "44100", "channel_count": "0"},
                {"name": "Mic", "type": "MME", "samplerate": "44100", "channel_count": "abc"},
                {"name": "Mic", "type": "MME", "samplerate": "44100", "channel_count": "inf"},
            ],
        )
    def test_start_invalid_saved_input_hardware_sets_visible_start_error(
            self,
            input_payload,
        ):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = _start_config()
            services.hss_service.load_io_devices.return_value = {
                "input": input_payload,
                "mic_cal": {},
            }
            mgr = TestManager(bus, services)

            assert mgr.start({"config_id": 1}) is False
            assert mgr.last_start_error() == "输入硬件配置无效，请重新保存硬件设备。"
            bus.test_started.emit.assert_not_called()

    def test_start_normalizes_integral_numeric_saved_input_channel_count_and_cleans_pending_start(
            self,
            monkeypatch,
        ):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = _start_config()
            services.hss_service.load_io_devices.return_value = {
                "input": {
                    "name": "Mic",
                    "type": "MME",
                    "samplerate": "44100",
                    "channel_count": "1.0",
                },
                "mic_cal": {},
            }

            class FakeRecorder(_CaptureRecorderDoubleMixin):
                instances = []

                def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                    self._sample_rate = sample_rate
                    self._n_channels = n_channels
                    self._record_duration = record_duration
                    self._buffer = np.zeros(
                        (int(sample_rate * record_duration), n_channels),
                        dtype=np.float32,
                    )
                    self.recording_done = _DummySignal()
                    self.error_occurred = _DummySignal()
                    self._running = False
                    FakeRecorder.instances.append(self)

                def isRunning(self):
                    return self._running

                def start(self):
                    self._running = True

                def stop(self):
                    self._running = False

                def wait(self, timeout):
                    return True

            monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
            monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

            mgr = TestManager(bus, services)
            request_id = "req-normalized-channel-count"

            accepted = mgr.start(
                {
                    "config_id": 1,
                    "sn": "SN1",
                    "factory_start_request_id": request_id,
                },
            )

            assert accepted is True
            assert FakeRecorder.instances[0]._n_channels == 1
            assert mgr.last_start_error() is None
            assert mgr._pending_factory_starts == {}
            bus.test_started.emit.assert_called_once_with(1)
            mgr.stop(config_id=1)

    @pytest.mark.parametrize(
            ("field", "value"),
            [
                ("input_device_name", ""),
                ("input_device_hostapi", " "),
                ("input_device_samplerate", "0"),
                ("input_device_samplerate", "abc"),
            ],
        )
    def test_start_missing_config_input_device_fields_sets_visible_start_error(
            self,
            field,
            value,
        ):
            bus = _mock_bus()
            services = _mock_services(bus)
            config = _start_config()
            config[field] = value
            services.config_service.load_config.return_value = config
            mgr = TestManager(bus, services)

            assert mgr.start({"config_id": 1}) is False
            assert mgr.last_start_error() == "配置未选择输入设备，请先在「配置」页面选择输入设备并保存。"
            bus.test_started.emit.assert_not_called()

    def test_start_absent_config_input_device_fields_sets_visible_start_error(self):
            bus = _mock_bus()
            services = _mock_services(bus)
            config = _start_config()
            for field in (
                "input_device_name",
                "input_device_hostapi",
                "input_device_samplerate",
            ):
                config.pop(field, None)
            services.config_service.load_config.return_value = config
            mgr = TestManager(bus, services)

            assert mgr.start({"config_id": 1}) is False
            assert mgr.last_start_error() == "配置未选择输入设备，请先在「配置」页面选择输入设备并保存。"
            bus.test_started.emit.assert_not_called()

    def test_start_monitor_failure_before_acceptance_preserves_completed_session_and_cleans_up(
            self,
            monkeypatch,
        ):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = {
                "config_id": 1,
                "config_name": "Test",
                "record_duration": 2.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "fft_nfft": 4096,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": "Speaker",
                "output_device_hostapi": "MME",
                "output_device_samplerate": 44100,
            }

            class FakeRecorder(_CaptureRecorderDoubleMixin):
                instances = []

                def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                    self._sample_rate = sample_rate
                    self._n_channels = n_channels
                    self._record_duration = record_duration
                    self._buffer = np.zeros(
                        (int(sample_rate * record_duration), n_channels),
                        dtype=np.float32,
                    )
                    self.recording_done = _DummySignal()
                    self.error_occurred = _DummySignal()
                    self._running = False
                    self.stop_calls = 0
                    FakeRecorder.instances.append(self)

                def isRunning(self):
                    return self._running

                def start(self):
                    raise RuntimeError("recorder boom")

                def stop(self):
                    self.stop_calls += 1
                    self._running = False

                def wait(self, timeout):
                    return True

            class FakeMonitor:
                instances = []

                def __init__(self, *, device_name, hostapi, sample_rate, monitor_channel):
                    self.error_occurred = _DummySignal()
                    self.stop_calls = 0
                    FakeMonitor.instances.append(self)

                def feed(self, chunk):
                    return None

                def start(self):
                    raise RuntimeError("monitor boom")

                def stop(self):
                    self.stop_calls += 1

                def wait(self, timeout):
                    return True

            monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
            monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", FakeMonitor)
            monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

            prior_session = MagicMock()
            mgr = TestManager(bus, services)
            mgr._completed_sessions[1] = prior_session
            accepted_run_ids: list[str] = []

            try:
                accepted = mgr.start(
                    {"config_id": 1, "sn": "SN1"},
                    on_start_accepted=accepted_run_ids.append,
                )
            except RuntimeError as exc:
                accepted = exc

            assert accepted is False
            assert mgr.last_start_error() == "启动失败：monitor boom"
            assert accepted_run_ids == []
            assert mgr._completed_sessions[1] is prior_session
            assert mgr.is_running(config_id=1) is False
            assert 1 not in mgr._recorders
            assert mgr._shared_recorders == {}
            assert FakeRecorder.instances[0].stop_calls == 1
            assert FakeMonitor.instances[0].stop_calls == 1
            bus.factory_runtime_aborted.emit.assert_not_called()
            bus.test_started.emit.assert_not_called()

    def test_start_output_sample_rate_setup_failure_before_acceptance_cleans_partial_runtime(
            self,
            monkeypatch,
        ):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = _start_config(
                output_device_name="Speaker",
                output_device_hostapi="MME",
                output_device_samplerate="bad",
            )

            class FakeRecorder(_CaptureRecorderDoubleMixin):
                instances = []

                def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                    self._sample_rate = sample_rate
                    self._n_channels = n_channels
                    self._record_duration = record_duration
                    self._buffer = np.zeros(
                        (int(sample_rate * record_duration), n_channels),
                        dtype=np.float32,
                    )
                    self.recording_done = _DummySignal()
                    self.error_occurred = _DummySignal()
                    self._running = False
                    self.stop_calls = 0
                    FakeRecorder.instances.append(self)

                def isRunning(self):
                    return self._running

                def start(self):
                    raise AssertionError("recorder should not start before acceptance")

                def stop(self):
                    self.stop_calls += 1
                    self._running = False

                def wait(self, timeout):
                    return True

            monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
            monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

            mgr = TestManager(bus, services)
            accepted_run_ids: list[str] = []

            accepted = mgr.start(
                {"config_id": 1, "sn": "SN1"},
                on_start_accepted=accepted_run_ids.append,
            )

            assert accepted is False
            assert mgr.last_start_error().startswith("启动失败：")
            assert "bad" in mgr.last_start_error()
            assert accepted_run_ids == []
            bus.test_started.emit.assert_not_called()
            bus.factory_runtime_aborted.emit.assert_not_called()
            assert mgr.is_running(config_id=1) is False
            assert mgr._recorders == {}
            assert mgr._monitors == {}
            assert mgr._shared_recorders == {}
            assert FakeRecorder.instances[0].stop_calls == 1

    def test_start_calls_on_start_accepted_with_factory_run_id_before_recorder_start_and_before_test_started(
            self,
            monkeypatch,
        ):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = {
                "config_id": 1,
                "config_name": "Test",
                "record_duration": 2.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "fft_nfft": 4096,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            }

            events: list[str] = []

            class FakeRecorder(_CaptureRecorderDoubleMixin):
                instances = []

                def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                    self.device_name = device_name
                    self.hostapi = hostapi
                    self._sample_rate = sample_rate
                    self._n_channels = n_channels
                    self._record_duration = record_duration
                    self._buffer = np.zeros(
                        (int(sample_rate * record_duration), n_channels),
                        dtype=np.float32,
                    )
                    self.recording_done = _DummySignal()
                    self.error_occurred = _DummySignal()
                    self._running = False
                    FakeRecorder.instances.append(self)

                def isRunning(self):
                    return self._running

                def start(self):
                    events.append("recorder.start")
                    self._running = True

                def stop(self):
                    self._running = False

                def wait(self, timeout):
                    return True

            monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
            monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

            prior_session = MagicMock()
            mgr = TestManager(bus, services)
            mgr._completed_sessions[1] = prior_session
            bus.test_started.emit.side_effect = lambda config_id: events.append(
                f"test_started:{config_id}"
            )
            accepted_snapshots: list[dict[str, object]] = []

            def on_start_accepted(run_id):
                accepted_snapshots.append(
                    {
                        "completed_session": mgr._completed_sessions.get(1),
                        "events": list(events),
                        "recorder_running": FakeRecorder.instances[0]._running,
                        "run_id": run_id,
                        "session_run_id": mgr._sessions[1]._workorder["factory_run_id"],
                    }
                )
                events.append("accepted")

            accepted = mgr.start(
                {"config_id": 1, "sn": "SN1"},
                on_start_accepted=on_start_accepted,
            )

            assert accepted is True
            assert accepted_snapshots == [
                {
                    "completed_session": prior_session,
                    "events": [],
                    "recorder_running": False,
                    "run_id": accepted_snapshots[0]["run_id"],
                    "session_run_id": accepted_snapshots[0]["session_run_id"],
                }
            ]
            assert isinstance(accepted_snapshots[0]["run_id"], str)
            assert accepted_snapshots[0]["run_id"]
            assert accepted_snapshots[0]["run_id"] == accepted_snapshots[0]["session_run_id"]
            assert events == ["accepted", "recorder.start", "test_started:1"]
            assert 1 not in mgr._completed_sessions
            assert FakeRecorder.instances[0]._running is True

    def test_start_on_start_accepted_exception_sets_visible_start_error(
            self,
            monkeypatch,
        ):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = _start_config()

            class FakeRecorder(_CaptureRecorderDoubleMixin):
                instances = []

                def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                    self._sample_rate = sample_rate
                    self._n_channels = n_channels
                    self._record_duration = record_duration
                    self._buffer = np.zeros(
                        (int(sample_rate * record_duration), n_channels),
                        dtype=np.float32,
                    )
                    self.recording_done = _DummySignal()
                    self.error_occurred = _DummySignal()
                    self._running = False
                    FakeRecorder.instances.append(self)

                def isRunning(self):
                    return self._running

                def start(self):
                    self._running = True

                def stop(self):
                    self._running = False

                def wait(self, timeout):
                    return True

            monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
            monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

            mgr = TestManager(bus, services)

            def on_start_accepted(_run_id):
                raise RuntimeError("accept failed")

            accepted = mgr.start(
                {"config_id": 1, "sn": "SN1"},
                on_start_accepted=on_start_accepted,
            )

            assert accepted is False
            assert mgr.last_start_error() == "启动失败：accept failed"
            assert mgr.is_running(config_id=1) is False
            bus.test_started.emit.assert_not_called()

    def test_recorder_error_during_current_collection_setup_fails_before_acceptance(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = _start_config()
        recorder_type = _install_start_boundary_doubles(monkeypatch)
        mgr = TestManager(bus, services)
        accepted_run_ids = []

        def fail_at_current_collection_entry(config_id, session):
            assert mgr._sessions.get(config_id) is session
            recorder_type.instances[0].error_occurred.emit(
                "current collection setup failed"
            )

        monkeypatch.setattr(
            mgr,
            "_start_current_collection",
            fail_at_current_collection_entry,
        )

        accepted = mgr.start(
            {"config_id": 1, "sn": "SN1"},
            on_start_accepted=accepted_run_ids.append,
        )

        assert accepted is False
        assert accepted_run_ids == []
        assert mgr.is_running(config_id=1) is False
        assert recorder_type.instances[0].start_calls == 0
        bus.test_started.emit.assert_not_called()

    def test_recorder_error_inside_acceptance_callback_aborts_at_startup_boundary(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = _start_config()
        recorder_type = _install_start_boundary_doubles(monkeypatch)
        mgr = TestManager(bus, services)
        accepted_run_ids = []

        def on_start_accepted(run_id):
            accepted_run_ids.append(run_id)
            recorder_type.instances[0].error_occurred.emit(
                "acceptance boundary failed"
            )

        accepted = mgr.start(
            {"config_id": 1, "sn": "SN1"},
            on_start_accepted=on_start_accepted,
        )

        assert accepted is False
        assert len(accepted_run_ids) == 1
        assert mgr.is_running(config_id=1) is False
        assert recorder_type.instances[0].start_calls == 0
        assert recorder_type.instances[0].stop_calls == 1
        bus.factory_runtime_aborted.emit.assert_called_once()
        bus.test_started.emit.assert_not_called()

    def test_acceptance_callback_can_stop_reentrantly_without_starting_recorder(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = _start_config()
        recorder_type = _install_start_boundary_doubles(monkeypatch)
        mgr = TestManager(bus, services)
        accepted_run_ids = []

        def on_start_accepted(run_id):
            accepted_run_ids.append(run_id)
            mgr.stop(config_id=1, silent=True)

        accepted = mgr.start(
            {"config_id": 1, "sn": "SN1"},
            on_start_accepted=on_start_accepted,
        )

        assert accepted is False
        assert len(accepted_run_ids) == 1
        assert mgr.is_running(config_id=1) is False
        assert recorder_type.instances[0].start_calls == 0
        assert recorder_type.instances[0].stop_calls == 1
        bus.factory_runtime_aborted.emit.assert_called_once()
        assert (
            bus.factory_runtime_aborted.emit.call_args.args[0].error_message
            is None
        )
        bus.test_started.emit.assert_not_called()

    def test_start_can_be_canceled_by_request_id_after_backend_session_exists_but_before_recorder_start(
            self,
            monkeypatch,
        ):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = {
                "config_id": 1,
                "config_name": "Test",
                "record_duration": 2.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "fft_nfft": 4096,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            }

            events: list[str] = []

            class FakeRecorder(_CaptureRecorderDoubleMixin):
                instances = []

                def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                    self._sample_rate = sample_rate
                    self._n_channels = n_channels
                    self._record_duration = record_duration
                    self._buffer = np.zeros(
                        (int(sample_rate * record_duration), n_channels),
                        dtype=np.float32,
                    )
                    self.recording_done = _DummySignal()
                    self.error_occurred = _DummySignal()
                    self._running = False
                    self.stop_calls = 0
                    FakeRecorder.instances.append(self)

                def isRunning(self):
                    return self._running

                def start(self):
                    events.append("recorder.start")
                    self._running = True

                def stop(self):
                    self.stop_calls += 1
                    self._running = False

                def wait(self, timeout):
                    return True

            monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
            monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

            mgr = TestManager(bus, services)
            cancel_pending_start = getattr(mgr, "cancel_pending_start", None)
            assert callable(cancel_pending_start)

            request_id = "req-1"

            def on_start_accepted(run_id):
                assert run_id
                assert mgr.is_running(config_id=1) is True
                assert cancel_pending_start(request_id=request_id, config_id=1) is True
                events.append("accepted")

            accepted = mgr.start(
                {
                    "config_id": 1,
                    "sn": "SN1",
                    "trigger_source": "manual",
                    "factory_start_request_id": request_id,
                },
                on_start_accepted=on_start_accepted,
            )

            assert accepted is False
            assert mgr.last_start_error() is None
            assert events == ["accepted"]
            assert mgr.is_running(config_id=1) is False
            assert 1 not in mgr._recorders
            assert FakeRecorder.instances[0].stop_calls == 1
            bus.test_started.emit.assert_not_called()

    def test_start_recorder_failure_after_acceptance_emits_factory_runtime_aborted_and_restores_completed_session(
            self,
            monkeypatch,
        ):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = {
                "config_id": 1,
                "config_name": "Test",
                "record_duration": 2.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "fft_nfft": 4096,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            }

            events: list[str] = []

            class FakeRecorder(_CaptureRecorderDoubleMixin):
                instances = []

                def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                    self._sample_rate = sample_rate
                    self._n_channels = n_channels
                    self._record_duration = record_duration
                    self._buffer = np.zeros(
                        (int(sample_rate * record_duration), n_channels),
                        dtype=np.float32,
                    )
                    self.recording_done = _DummySignal()
                    self.error_occurred = _DummySignal()
                    self._running = False
                    self.stop_calls = 0
                    FakeRecorder.instances.append(self)

                def isRunning(self):
                    return self._running

                def start(self):
                    events.append("recorder.start")
                    raise RuntimeError("recorder boom")

                def stop(self):
                    self.stop_calls += 1
                    self._running = False

                def wait(self, timeout):
                    return True

            monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
            monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

            prior_session = MagicMock()
            mgr = TestManager(bus, services)
            mgr._completed_sessions[1] = prior_session
            accepted_snapshots: list[dict[str, object]] = []

            def on_start_accepted(run_id):
                accepted_snapshots.append(
                    {
                        "completed_session": mgr._completed_sessions.get(1),
                        "events": list(events),
                        "recorder_running": FakeRecorder.instances[0]._running,
                        "run_id": run_id,
                        "session_run_id": mgr._sessions[1]._workorder["factory_run_id"],
                    }
                )
                events.append("accepted")

            accepted = mgr.start(
                {"config_id": 1, "sn": "SN1", "trigger_source": "manual"},
                on_start_accepted=on_start_accepted,
            )

            assert accepted is False
            assert mgr.last_start_error() is None
            assert accepted_snapshots == [
                {
                    "completed_session": prior_session,
                    "events": [],
                    "recorder_running": False,
                    "run_id": accepted_snapshots[0]["run_id"],
                    "session_run_id": accepted_snapshots[0]["session_run_id"],
                }
            ]
            assert accepted_snapshots[0]["run_id"] == accepted_snapshots[0]["session_run_id"]
            assert events == ["accepted", "recorder.start"]
            abort_payload = bus.factory_runtime_aborted.emit.call_args.args[0]
            assert abort_payload.config_id == 1
            assert abort_payload.run_id == accepted_snapshots[0]["run_id"]
            assert abort_payload.trigger_source == "manual"
            assert abort_payload.error_message == "recorder boom"
            assert mgr._completed_sessions[1] is prior_session
            assert mgr.is_running(config_id=1) is False
            assert 1 not in mgr._recorders
            assert mgr._shared_recorders == {}
            assert FakeRecorder.instances[0].stop_calls == 1
            bus.test_started.emit.assert_not_called()

    @pytest.mark.parametrize("completion_phase", ["acceptance", "recorder_start"])
    def test_start_keeps_newly_completed_session_when_recorder_finishes_before_start_returns(
            self,
            monkeypatch,
            completion_phase,
        ):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = {
                "config_id": 1,
                "config_name": "Test",
                "record_duration": 2.0,
                "analysis_duration": 1.0,
                "analysis_window_offset_ms": 0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "fft_nfft": 4096,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 100,
                "output_device_name": None,
            }
            events: list[str] = []

            class FastRecorder(_CaptureRecorderDoubleMixin):
                instances = []

                def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                    self._sample_rate = float(sample_rate)
                    self._n_channels = n_channels
                    self._record_duration = float(record_duration)
                    self._buffer = np.ones(
                        (int(self._sample_rate * self._record_duration), n_channels),
                        dtype=np.float32,
                    )
                    self.recording_done = _DummySignal()
                    self.error_occurred = _DummySignal()
                    self._running = False
                    self.start_calls = 0
                    FastRecorder.instances.append(self)

                def isRunning(self):
                    return self._running

                def start(self):
                    self.start_calls += 1
                    self._running = True
                    if completion_phase == "recorder_start":
                        self.captured_block_ready.emit(
                            CapturedAudioBlock(
                                data=np.ones((1, self._n_channels), dtype=np.float32),
                                sample_start=0,
                                sample_end=1,
                                monotonic_start=0.0,
                                monotonic_end=1.0 / self._sample_rate,
                            )
                        )

                def stop(self):
                    self._running = False

                def wait(self, timeout):
                    return True

            class FastSession(_FakeSession):
                def __init__(self, config, bus, ai_service, mic_cal, workorder, audio_dir):
                    super().__init__(config, bus, ai_service, mic_cal, workorder, audio_dir)
                    self._config = dict(config)
                    self._audio_file_path = None

                def _run_judgment(self):
                    self._audio_file_path = "captured.wav"
                    return JudgmentResult(
                        overall="合格",
                        limit_pass=True,
                        ai_pass=True,
                        ai_score=0.95,
                        analysis_window=(0.0, 1.0),
                    )

                def _on_captured_block(self, block, *, source_token):
                    if source_token is not self._source_token:
                        return
                    self._captured_frame_count = int(
                        self._sample_rate * self._config["record_duration"]
                    )
                    self._finished = True
                    self._on_session_done_cb(self.config_id)

            monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FastRecorder)
            monkeypatch.setattr("app.service.analysis.test_manager.TestSession", FastSession)

            prior_session = MagicMock(name="prior_session")
            mgr = TestManager(bus, services)
            mgr._completed_sessions[1] = prior_session
            bus.test_started.emit.side_effect = lambda config_id: events.append(
                f"test_started:{config_id}"
            )
            bus.status.emit.side_effect = lambda message: events.append(f"status:{message}")
            bus.log.emit.side_effect = lambda message: events.append(f"log:{message}")

            def on_start_accepted(_run_id):
                if completion_phase == "acceptance":
                    FastRecorder.instances[0].captured_block_ready.emit(object())

            accepted = mgr.start(
                {"config_id": 1, "sn": "SN1", "trigger_source": "manual"},
                on_start_accepted=on_start_accepted,
            )

            assert accepted is True
            assert mgr.is_running(config_id=1) is False
            assert mgr._completed_sessions[1] is not prior_session
            assert mgr._completed_sessions[1]._workorder["sn"] == "SN1"
            assert mgr._completed_sessions[1]._workorder["trigger_source"] == "manual"
            assert "status:Analyzing..." in events
            assert "status:Idle" in events
            assert "test_started:1" not in events
            assert "status:Recording..." not in events
            assert not any(
                event.startswith("log:[TestManager] started config=1")
                for event in events
            )
            assert FastRecorder.instances[0].start_calls == (
                0 if completion_phase == "acceptance" else 1
            )
            bus.factory_runtime_auto_analysis_succeeded.emit.assert_called_once()

    def test_start_does_not_emit_started_or_recording_status_after_reentrant_recorder_error(
            self,
            monkeypatch,
        ):
            bus = _mock_bus()
            services = _mock_services(bus)
            services.config_service.load_config.return_value = {
                "config_id": 1,
                "config_name": "Test",
                "record_duration": 2.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "fft_nfft": 4096,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 100,
                "output_device_name": None,
            }

            events: list[str] = []

            class ErrorRecorder(_CaptureRecorderDoubleMixin):
                instances = []

                def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                    self._sample_rate = float(sample_rate)
                    self._n_channels = n_channels
                    self._record_duration = float(record_duration)
                    self._buffer = np.ones(
                        (int(self._sample_rate * self._record_duration), n_channels),
                        dtype=np.float32,
                    )
                    self.recording_done = _DummySignal()
                    self.error_occurred = _DummySignal()
                    self._running = False
                    ErrorRecorder.instances.append(self)

                def isRunning(self):
                    return self._running

                def start(self):
                    self._running = True
                    self.error_occurred.emit("device lost")

                def stop(self):
                    self._running = False

                def wait(self, timeout):
                    return True

            monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", ErrorRecorder)
            monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

            prior_session = MagicMock(name="prior_session")
            mgr = TestManager(bus, services)
            mgr._completed_sessions[1] = prior_session
            bus.test_started.emit.side_effect = lambda config_id: events.append(
                f"test_started:{config_id}"
            )
            bus.status.emit.side_effect = lambda message: events.append(f"status:{message}")
            bus.log.emit.side_effect = lambda message: events.append(f"log:{message}")

            accepted = mgr.start({"config_id": 1, "sn": "SN1", "trigger_source": "manual"})

            assert accepted is True
            assert mgr.is_running(config_id=1) is False
            assert mgr._completed_sessions[1] is prior_session
            assert "status:Error: device lost" in events
            assert "test_started:1" not in events
            assert "status:Recording..." not in events
            assert not any(
                event.startswith("log:[TestManager] started config=1")
                for event in events
            )
            bus.factory_runtime_aborted.emit.assert_called_once()
