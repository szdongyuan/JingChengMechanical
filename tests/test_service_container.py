"""Test that ServiceContainer wires AiService."""
from __future__ import annotations

import inspect

import app.services_container as mod


def test_service_container_has_ai_service():
    source = inspect.getsource(mod.ServiceContainer.__post_init__)
    assert "ai_service" in source
    assert "AiService" in source
