"""Test that ServiceContainer wires AiService."""
from __future__ import annotations

import inspect

import app.services_container as mod
from app.services_container import EventBus


def test_service_container_has_ai_service():
    source = inspect.getsource(mod.ServiceContainer.__post_init__)
    assert "ai_service" in source
    assert "AiService" in source


def test_analysis_signals_exist():
    """All analysis kernel signals should be defined on EventBus."""
    for name in (
        "stft_updated",
        "spl_updated",
        "octave_updated",
        "fft_updated",
        "recording_progress",
        "test_started",
        "test_finished",
    ):
        assert hasattr(EventBus, name), f"Missing signal: {name}"
