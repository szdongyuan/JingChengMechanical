"""Tests for AiService — toolchain registry, binding resolution, binding CRUD."""
from __future__ import annotations

import math

import pytest

from model.test_config_table import TestConfigTable as ConfigTable
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable as AiBindingTable
from app.service.ai_service import AiService


@pytest.fixture
def ai_service():
    return AiService()


@pytest.fixture
def toolchain():
    return AiToolchainTable.create(
        toolchain_id="motor-ai",
        display_name="Motor AI",
        version="1.0",
        entrypoint="python motor_ai.py",
        default_timeout_ms=30000,
    )


@pytest.fixture
def config():
    return ConfigTable.create(config_name="Cfg1")


class TestToolchainRegistry:
    def test_list_enabled_toolchains(self, ai_service, toolchain):
        result = ai_service.list_enabled_toolchains()
        assert len(result) == 1
        assert result[0]["toolchain_id"] == "motor-ai"
        assert result[0]["display_name"] == "Motor AI"

    def test_list_excludes_disabled(self, ai_service):
        AiToolchainTable.create(
            toolchain_id="disabled-tc",
            display_name="Disabled",
            version="1",
            entrypoint="x",
            enabled=False,
        )
        assert ai_service.list_enabled_toolchains() == []

    def test_get_toolchain(self, ai_service, toolchain):
        result = ai_service.get_toolchain("motor-ai")
        assert result is not None
        assert result.display_name == "Motor AI"

    def test_get_toolchain_not_found(self, ai_service):
        assert ai_service.get_toolchain("nonexistent") is None


class TestBindingResolver:
    def test_resolve_binding_exists(self, ai_service, config, toolchain):
        AiBindingTable.create(
            config=config, toolchain=toolchain, enabled=True, timeout_ms=5000
        )
        binding = ai_service.resolve_binding(config.config_id)
        assert binding is not None
        assert binding["enabled"] is True
        assert binding["toolchain_id"] == "motor-ai"
        assert binding["timeout_ms"] == 5000

    def test_resolve_binding_excludes_analysis_window_offset_ms(
        self, ai_service, config, toolchain
    ):
        AiBindingTable.create(
            config=config, toolchain=toolchain, enabled=True, timeout_ms=5000
        )

        binding = ai_service.resolve_binding(config.config_id)

        assert "analysis_window_offset_ms" not in binding

    def test_resolve_binding_not_found(self, ai_service, config):
        assert ai_service.resolve_binding(config.config_id) is None

    def test_resolve_binding_inherits_toolchain_timeout(self, ai_service, config, toolchain):
        AiBindingTable.create(
            config=config, toolchain=toolchain, enabled=True
        )
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["timeout_ms"] is None
        assert binding["effective_timeout_ms"] == 30000

    def test_resolve_binding_includes_score_threshold(self, ai_service, config, toolchain):
        AiBindingTable.create(
            config=config, toolchain=toolchain, enabled=True, score_threshold=0.8
        )
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["score_threshold"] == 0.8

    def test_resolve_binding_score_threshold_defaults_none(self, ai_service, config, toolchain):
        AiBindingTable.create(
            config=config, toolchain=toolchain, enabled=True
        )
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["score_threshold"] is None

    def test_resolve_binding_with_null_toolchain(self, ai_service):
        """Binding with no toolchain should still be resolvable."""
        cfg = ConfigTable.create(config_name="NullTC")
        AiBindingTable.create(config=cfg, toolchain=None, enabled=False)
        binding = ai_service.resolve_binding(cfg.config_id)
        assert binding is not None
        assert binding["toolchain_id"] is None
        assert binding["enabled"] is False


class TestBindingCrud:
    def test_save_creates_new_binding(self, ai_service, config, toolchain):
        bid = ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "timeout_ms": 5000,
            "params_json": '{"k": "v"}',
        })
        assert bid > 0
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["enabled"] is True
        assert binding["timeout_ms"] == 5000
        assert binding["params_json"] == '{"k": "v"}'

    def test_save_binding_ignores_analysis_window_offset_ms(
        self, ai_service, config, toolchain
    ):
        bid = ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "timeout_ms": 5000,
            "analysis_window_offset_ms": 500,
            "params_json": '{"k": "v"}',
        })

        assert bid > 0

        binding = ai_service.resolve_binding(config.config_id)
        row = AiBindingTable.get(AiBindingTable.config == config.config_id)

        assert "analysis_window_offset_ms" not in binding
        assert not hasattr(row, "analysis_window_offset_ms")

    def test_save_updates_existing_binding(self, ai_service, config, toolchain):
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
        })
        ai_service.save_binding(config.config_id, {
            "enabled": False,
            "toolchain_id": "motor-ai",
            "timeout_ms": 9999,
        })
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["enabled"] is False
        assert binding["timeout_ms"] == 9999

    def test_clear_binding_preserves_row(self, ai_service, config, toolchain):
        """Clearing a binding keeps the row but nulls toolchain and disables."""
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
        })
        ai_service.clear_binding(config.config_id)
        binding = ai_service.resolve_binding(config.config_id)
        assert binding is not None  # row still exists
        assert binding["toolchain_id"] is None
        assert binding["enabled"] is False

    def test_clear_nonexistent_binding_is_noop(self, ai_service, config):
        """Clearing when no binding exists does nothing and doesn't error."""
        ai_service.clear_binding(config.config_id)  # should not raise


class TestScoreThresholdCrud:
    """score_threshold validation and persistence in save_binding()."""

    def test_save_binding_round_trip_with_score_threshold(self, ai_service, config, toolchain):
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "score_threshold": 0.8,
        })
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["score_threshold"] == 0.8

    def test_save_binding_updates_existing_threshold(self, ai_service, config, toolchain):
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "score_threshold": 0.8,
        })
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "score_threshold": 0.5,
        })
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["score_threshold"] == 0.5

    def test_save_binding_absent_key_preserves_existing_threshold(self, ai_service, config, toolchain):
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "score_threshold": 0.8,
        })
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
        })
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["score_threshold"] == 0.8

    def test_save_binding_explicit_none_clears_threshold(self, ai_service, config, toolchain):
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "score_threshold": 0.8,
        })
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "score_threshold": None,
        })
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["score_threshold"] is None

    def test_save_binding_rejects_bool_threshold(self, ai_service, config, toolchain):
        with pytest.raises(ValueError, match="score_threshold"):
            ai_service.save_binding(config.config_id, {
                "enabled": True,
                "score_threshold": True,
            })

    def test_save_binding_rejects_string_threshold(self, ai_service, config, toolchain):
        with pytest.raises(ValueError, match="score_threshold"):
            ai_service.save_binding(config.config_id, {
                "enabled": True,
                "score_threshold": "0.8",
            })

    def test_save_binding_rejects_nan_threshold(self, ai_service, config, toolchain):
        nan_value = math.nan
        with pytest.raises(ValueError, match="score_threshold"):
            ai_service.save_binding(config.config_id, {
                "enabled": True,
                "score_threshold": nan_value,
            })

    def test_save_binding_rejects_inf_threshold(self, ai_service, config, toolchain):
        inf_value = math.inf
        with pytest.raises(ValueError, match="score_threshold"):
            ai_service.save_binding(config.config_id, {
                "enabled": True,
                "score_threshold": inf_value,
            })
