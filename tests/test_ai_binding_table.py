"""Tests for TestConfigAiBindingTable model."""
from __future__ import annotations

import pytest
from peewee import IntegrityError

from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable as AiBindingTable
from model.test_config_table import TestConfigTable as ConfigTable


@pytest.fixture
def config_and_toolchain():
    cfg = ConfigTable.create(config_name="Test Config 1")
    tc = AiToolchainTable.create(
        toolchain_id="tc-1", display_name="TC1", version="1", entrypoint="echo"
    )
    return cfg, tc


class TestAiBindingTable:
    def test_create_and_read(self, config_and_toolchain):
        cfg, tc = config_and_toolchain
        binding = AiBindingTable.create(
            config=cfg,
            enabled=True,
            toolchain=tc,
            timeout_ms=60000,
            analysis_window_offset_ms=1000,
            params_json='{"threshold": 0.5}',
        )
        fetched = AiBindingTable.get_by_id(binding.binding_id)
        assert fetched.config_id == cfg.config_id
        assert fetched.enabled is True
        assert fetched.toolchain_id == tc.toolchain_id
        assert fetched.timeout_ms == 60000
        assert fetched.analysis_window_offset_ms == 1000
        assert fetched.params_json == '{"threshold": 0.5}'

    def test_defaults(self, config_and_toolchain):
        cfg, tc = config_and_toolchain
        binding = AiBindingTable.create(config=cfg, toolchain=tc)
        assert binding.enabled is False
        assert binding.timeout_ms is None
        assert binding.analysis_window_offset_ms == 0
        assert binding.params_json == "{}"
        assert binding.updated_time is not None
        assert len(binding.updated_time) > 0

    def test_one_binding_per_config(self, config_and_toolchain):
        """Each test config can have at most one AI binding (unique constraint on config)."""
        cfg, tc = config_and_toolchain
        AiBindingTable.create(config=cfg, toolchain=tc)
        tc2 = AiToolchainTable.create(
            toolchain_id="tc-2", display_name="TC2", version="1", entrypoint="echo2"
        )
        with pytest.raises(IntegrityError):
            AiBindingTable.create(config=cfg, toolchain=tc2)

    def test_cascade_delete_with_config(self, config_and_toolchain):
        """Deleting a test config cascades to its AI binding."""
        cfg, tc = config_and_toolchain
        AiBindingTable.create(config=cfg, toolchain=tc, enabled=True)
        assert AiBindingTable.select().count() == 1
        cfg.delete_instance()
        assert AiBindingTable.select().count() == 0

    def test_nullable_toolchain(self):
        """Binding can exist with no toolchain selected (not_configured state)."""
        cfg = ConfigTable.create(config_name="No TC Cfg")
        binding = AiBindingTable.create(
            config=cfg, toolchain=None, enabled=False
        )
        fetched = AiBindingTable.get_by_id(binding.binding_id)
        assert fetched.toolchain_id is None
        assert fetched.enabled is False

    def test_toolchain_delete_sets_null(self, config_and_toolchain):
        """Deleting a toolchain sets the binding's toolchain to NULL, not cascade-delete."""
        cfg, tc = config_and_toolchain
        AiBindingTable.create(config=cfg, toolchain=tc, enabled=True)
        tc.delete_instance()
        fetched = AiBindingTable.select().where(
            AiBindingTable.config == cfg.config_id
        ).first()
        assert fetched is not None
        assert fetched.toolchain_id is None
