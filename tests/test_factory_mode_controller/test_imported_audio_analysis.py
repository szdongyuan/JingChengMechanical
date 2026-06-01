from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from scipy.io import wavfile
import app.services_container as services_mod
from app.service.analysis.imported_audio import (
    ImportedAudioAnalysisRequest,
    ImportedChannelAssignment,
)
from app.service.analysis.test_manager import TestManager
from app.services_container import EventBus
from model.audio_data_table import AudioDataTable

from ._support import (
    _load_factory_mode_controller_module,
    _make_config,
    _make_services,
)


def _request() -> ImportedAudioAnalysisRequest:
    return ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(ImportedChannelAssignment(0, "config", 0),),
    )


def _import_smoke_config(config_id: int = 101) -> dict:
    return {
        "config_id": config_id,
        "config_name": f"Config {config_id}",
        "record_duration": 1.0,
        "analysis_duration": 0.1,
        "analysis_window_offset_ms": 0,
        "selected_channels": [0],
        "channels": [
            {
                "channel_index": 0,
                "stft_nfft": 128,
                "fft_nfft": 128,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_lower_enabled": False,
                "spl_lower_limit": 40.0,
                "spl_upper_enabled": False,
                "spl_upper_limit": 85.0,
                "spl_average_lower_enabled": False,
                "spl_average_lower_limit": 40.0,
                "spl_average_upper_enabled": False,
                "spl_average_upper_limit": 85.0,
                "loudness_unit": "sone",
                "loudness_lower_enabled": False,
                "loudness_lower_limit": 0.0,
                "loudness_upper_enabled": False,
                "loudness_upper_limit": 60.0,
                "loudness_average_lower_enabled": False,
                "loudness_average_lower_limit": 0.0,
                "loudness_average_upper_enabled": False,
                "loudness_average_upper_limit": 60.0,
                "fft_band_limits": [],
                "fft_golden_mode": "off",
                "fft_show_golden_overlay": False,
                "oct_weighting": "Z",
                "oct_band_limits": [],
                "oct_golden_mode": "off",
                "oct_show_golden_overlay": False,
            }
        ],
        "input_device_name": "InputMic",
        "input_device_hostapi": "InputAPI",
        "input_device_samplerate": 48000,
        "output_device_name": "OutputSpeaker",
        "output_device_hostapi": "OutputAPI",
        "output_device_samplerate": 48000,
    }


def _make_realistic_services_for_import():
    bus = EventBus()
    stored_config = _import_smoke_config(101)
    services = SimpleNamespace()
    services.bus = bus
    services.config_service = MagicMock()
    services.config_service.load_config.side_effect = lambda config_id: copy.deepcopy(
        stored_config
    )
    services.config_service.list_configs.return_value = [
        {"config_id": 101, "config_name": stored_config["config_name"]}
    ]
    services.config_service.validate_factory_runtime.return_value = []
    services.config_service.golden_sample_service = None
    services.hss_service = MagicMock()
    services.hss_service.load_io_devices.return_value = {"mic_cal": {}}
    services.ai_service = MagicMock()
    services.factory_selection_settings = MagicMock()
    services.factory_selection_settings.save_selected_config_ids.return_value = None
    services.test = TestManager(bus, services)
    return services


def test_imported_audio_controller_smoke_is_temporary_and_offline(
    monkeypatch,
    tmp_path,
):
    wav_path = tmp_path / "import.wav"
    wavfile.write(wav_path, 25600, np.zeros((2560, 1), dtype=np.float32))
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path=str(wav_path),
        assignments=(ImportedChannelAssignment(0, "config", 0),),
    )
    services = _make_realistic_services_for_import()
    controller_mod = _load_factory_mode_controller_module()
    controller = controller_mod.FactoryModeController(services.bus, services)
    controller.set_selection((101,))

    started_signals: list[tuple] = []
    finished_signals: list[tuple] = []
    result_changed_signals: list[tuple] = []
    services.bus.test_started.connect(lambda *args: started_signals.append(args))
    services.bus.test_finished.connect(lambda *args: finished_signals.append(args))
    services.bus.result_changed.connect(
        lambda *args: result_changed_signals.append(args)
    )

    save_wav = MagicMock(side_effect=AssertionError("import must not save wav"))
    audio_recorder = MagicMock(
        side_effect=AssertionError("import must not instantiate AudioRecorder")
    )
    input_stream = MagicMock(
        side_effect=AssertionError("import must not open hardware input stream")
    )
    create_audio = MagicMock(
        side_effect=AssertionError("import must not create history row")
    )
    monkeypatch.setattr(
        "app.service.analysis.test_session.TestSession._save_wav",
        save_wav,
        raising=False,
    )
    monkeypatch.setattr(
        "app.service.analysis.test_manager.AudioRecorder",
        audio_recorder,
        raising=False,
    )
    monkeypatch.setattr(
        "app.service.analysis.audio_recorder.sd.InputStream",
        input_stream,
        raising=False,
    )
    monkeypatch.setattr(
        "app.service.analysis.test_session.AudioDataTable.create",
        create_audio,
        raising=False,
    )
    services.config_service.save_config = MagicMock(
        side_effect=AssertionError("import mapping must not be saved")
    )
    services.config_service.begin_save_config = MagicMock(
        side_effect=AssertionError("import mapping must not be staged")
    )
    services.ai_service.resolve_binding = MagicMock(
        side_effect=AssertionError("import must not resolve AI binding")
    )
    services.ai_service.run_ai_analysis = MagicMock(
        side_effect=AssertionError("import must not run AI")
    )
    services.workorder_result_service = MagicMock()
    services.barcode_result_service = MagicMock()
    services.production_result_service = MagicMock()

    accepted = controller.analyze_imported_audio(request)

    assert accepted is True
    assert controller.review_payload.trigger_source == "import_audio"
    assert controller.review_payload.displayed_config_id == 101
    assert controller.review_payload.run_id.startswith("import_101_")
    assert isinstance(controller.review_payload.import_warnings, tuple)
    assert controller.review_payload.chart_payload
    fft_payload = controller.review_payload.chart_payload[0]["fft"]
    frequencies_hz = fft_payload["frequencies_hz"]
    assert frequencies_hz
    assert max(frequencies_hz) <= 12800.0
    assert max(frequencies_hz) > 12000.0
    assert AudioDataTable.select().count() == 0
    assert "barcode" not in request.__dict__
    assert "workorder" not in request.__dict__
    assert services.workorder_result_service.mock_calls == []
    assert services.barcode_result_service.mock_calls == []
    assert services.production_result_service.mock_calls == []
    services.config_service.save_config.assert_not_called()
    services.config_service.begin_save_config.assert_not_called()
    services.ai_service.resolve_binding.assert_not_called()
    services.ai_service.run_ai_analysis.assert_not_called()
    save_wav.assert_not_called()
    audio_recorder.assert_not_called()
    input_stream.assert_not_called()
    create_audio.assert_not_called()
    assert started_signals == []
    assert finished_signals == []
    assert result_changed_signals == []


def test_factory_controller_applies_imported_audio_payload():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101),))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101,))
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    payload = services_mod.FactoryReviewPayload(
        displayed_config_id=101,
        run_id="import-101",
        trigger_source="import_audio",
        summary_payload={"overall_status": "pass", "rows": []},
        chart_payload={0: {"spl": {}}},
    )
    services.test.analyze_imported_wav.return_value = payload
    request = _request()

    assert controller.analyze_imported_audio(request) is True

    services.test.analyze_imported_wav.assert_called_once_with(request)
    services.test.start.assert_not_called()
    services.test.stop.assert_not_called()
    assert controller.state.run_phase == "reviewing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.trigger_source == "import_audio"
    assert controller.review_payload == payload
    assert reviews[-1].trigger_source == "import_audio"
    assert states[-1] == controller.state


def test_factory_controller_reanalyzes_imported_audio_with_selected_window():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101),))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101,))
    request = _request()
    first_payload = services_mod.FactoryReviewPayload(
        displayed_config_id=101,
        run_id="import-101-a",
        trigger_source="import_audio",
        captured_duration_sec=2.0,
        default_window=services_mod.FactoryReviewWindow(0.0, 0.5),
        fallback_window=services_mod.FactoryReviewWindow(0.0, 2.0),
        manual_reanalysis_allowed=True,
        judgment_payload={"overall": "PASS", "analysis_window": (0.0, 0.5)},
        summary_payload={"overall_status": "pass", "rows": []},
        chart_payload={0: {"spl": {}}},
    )
    second_payload = services_mod.FactoryReviewPayload(
        displayed_config_id=101,
        run_id="import-101-b",
        trigger_source="import_audio",
        captured_duration_sec=2.0,
        default_window=services_mod.FactoryReviewWindow(0.25, 0.65),
        fallback_window=services_mod.FactoryReviewWindow(0.0, 2.0),
        manual_reanalysis_allowed=True,
        judgment_payload={"overall": "PASS", "analysis_window": (0.25, 0.65)},
        summary_payload={"overall_status": "pass", "rows": []},
        chart_payload={0: {"spl": {}}},
    )
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services.test.analyze_imported_wav.side_effect = [first_payload, second_payload]

    assert controller.analyze_imported_audio(request) is True
    assert controller.begin_reanalysis(0.25, 0.65) is True

    assert services.test.analyze_imported_wav.call_count == 2
    reanalysis_request = services.test.analyze_imported_wav.call_args_list[1].args[0]
    assert reanalysis_request is not request
    assert reanalysis_request.config_id == 101
    assert reanalysis_request.wav_path == request.wav_path
    assert reanalysis_request.assignments == request.assignments
    assert reanalysis_request.start_sec == pytest.approx(0.25)
    assert reanalysis_request.duration_sec == pytest.approx(0.4)
    services.test.rejudge.assert_not_called()
    assert controller.state.run_phase == "reviewing"
    assert controller.state.trigger_source == "import_audio"
    assert controller.review_payload == second_payload
    assert any(state.run_phase == "reanalyzing" for state in states)
    assert reviews[-1] == second_payload


def test_factory_controller_rejects_imported_audio_while_running():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services((_make_config(101),))
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((101,))
    controller._state = replace(controller.state, run_phase="running")

    assert controller.analyze_imported_audio(_request()) is False

    services.test.analyze_imported_wav.assert_not_called()
    services.test.start.assert_not_called()


def test_factory_controller_rejects_imported_audio_while_pending_start():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services((_make_config(101),))
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((101,))
    controller._pending_start = (101, "manual")
    controller._pending_start_request_id = "factory-start-pending"

    assert controller.analyze_imported_audio(_request()) is False

    services.test.analyze_imported_wav.assert_not_called()
    services.test.start.assert_not_called()


def test_factory_controller_import_error_enters_no_result_review():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101),))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101,))
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services.test.analyze_imported_wav.side_effect = ValueError("WAV 为空")

    assert controller.analyze_imported_audio(_request()) is False

    services.test.start.assert_not_called()
    assert controller.state.run_phase == "reviewing"
    assert controller.state.displayed_config_id == 101
    assert controller.review_payload.no_result is True
    assert controller.review_payload.trigger_source == "import_audio"
    assert "WAV 为空" in controller.review_payload.error_message
    assert controller.review_payload.summary_payload["overall_status"] == "no_result"
    assert "WAV 为空" in controller.review_payload.summary_payload["error_message"]
    assert reviews[-1] == controller.review_payload
    assert states[-1] == controller.state
