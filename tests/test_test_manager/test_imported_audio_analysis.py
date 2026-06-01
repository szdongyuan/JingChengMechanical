from __future__ import annotations

from unittest.mock import MagicMock

from app.service.analysis.imported_audio import (
    ImportedAudioAnalysisRequest,
    ImportedChannelAssignment,
)
from app.service.analysis.judgment import JudgmentEngine
from app.service.analysis.test_manager import TestManager
from app.services_container import FactoryReviewPayload

from ._support import _factory_runtime_config, _mock_bus, _mock_services


def test_test_manager_analyze_imported_wav_returns_payload_without_persistence(
    monkeypatch,
    tmp_path,
):
    bus = _mock_bus()
    services = _mock_services(bus)
    config = _factory_runtime_config(config_id=101)
    mic_cal = {"In1": {"sensitivity": 42.0}}
    golden_sample_service = object()
    services.config_service.load_config.return_value = config
    services.config_service.golden_sample_service = golden_sample_service
    services.hss_service.load_io_devices.return_value = {"mic_cal": mic_cal}
    mgr = TestManager(bus, services)
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path=str(tmp_path / "import.wav"),
        assignments=(ImportedChannelAssignment(0, "config", 0),),
    )
    expected_payload = FactoryReviewPayload(
        displayed_config_id=101,
        run_id="import-101",
        trigger_source="import_audio",
        summary_payload={"overall_status": "pass", "rows": []},
        chart_payload={0: {"spl": {}}},
    )

    create_audio = MagicMock(side_effect=AssertionError("must not persist"))
    save_wav = MagicMock(side_effect=AssertionError("must not save wav"))
    captured: dict[str, object] = {}

    def fake_analyze_imported_wav(**kwargs):
        captured.update(kwargs)
        return expected_payload

    monkeypatch.setattr(
        "app.service.analysis.test_session.AudioDataTable.create",
        create_audio,
        raising=False,
    )
    monkeypatch.setattr(
        "app.service.analysis.test_session.TestSession._save_wav",
        save_wav,
        raising=False,
    )
    monkeypatch.setattr(
        "app.service.analysis.test_manager.AudioRecorder",
        MagicMock(side_effect=AssertionError("must not create recorder")),
    )
    monkeypatch.setattr(
        "app.service.analysis.test_manager.AudioMonitor",
        MagicMock(side_effect=AssertionError("must not create monitor")),
    )
    monkeypatch.setattr(
        "app.service.analysis.test_manager.TestSession",
        MagicMock(side_effect=AssertionError("must not create session")),
    )
    monkeypatch.setattr(
        "app.service.analysis.imported_audio.analyze_imported_wav",
        fake_analyze_imported_wav,
    )

    payload = mgr.analyze_imported_wav(request)

    assert payload is expected_payload
    services.config_service.load_config.assert_called_once_with(101)
    services.hss_service.load_io_devices.assert_called_once_with()
    assert captured["request"] is request
    assert captured["config"] is config
    assert captured["mic_cal"] is mic_cal
    judgment_engine = captured["judgment_engine"]
    assert isinstance(judgment_engine, JudgmentEngine)
    assert judgment_engine._ai is services.ai_service
    assert judgment_engine._golden_sample_service is golden_sample_service
    create_audio.assert_not_called()
    save_wav.assert_not_called()
    bus.test_started.emit.assert_not_called()
    bus.test_finished.emit.assert_not_called()
    bus.result_changed.emit.assert_not_called()
    services.ai_service.resolve_binding.assert_not_called()
