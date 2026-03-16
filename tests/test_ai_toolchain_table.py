"""Tests for AiToolchainTable model."""
from __future__ import annotations

import pytest
from peewee import IntegrityError  # used by test_toolchain_id_is_primary_key

from model.ai_toolchain_table import AiToolchainTable


class TestAiToolchainTable:
    def test_create_and_read(self):
        row = AiToolchainTable.create(
            toolchain_id="motor-dual-segment-ai",
            display_name="Motor AI v1",
            version="1.0.0",
            entrypoint="python ai_motor.py",
            protocol_version=1,
            execution_mode="oneshot_process",
            default_timeout_ms=30000,
            enabled=True,
            description="Motor dual-segment AI toolchain",
        )
        fetched = AiToolchainTable.get_by_id("motor-dual-segment-ai")
        assert fetched.display_name == "Motor AI v1"
        assert fetched.version == "1.0.0"
        assert fetched.entrypoint == "python ai_motor.py"
        assert fetched.protocol_version == 1
        assert fetched.execution_mode == "oneshot_process"
        assert fetched.default_timeout_ms == 30000
        assert fetched.enabled is True
        assert fetched.description == "Motor dual-segment AI toolchain"

    def test_defaults(self):
        row = AiToolchainTable.create(
            toolchain_id="minimal-tc",
            display_name="Minimal",
            entrypoint="echo ok",
        )
        fetched = AiToolchainTable.get_by_id("minimal-tc")
        assert fetched.enabled is True
        assert fetched.protocol_version == 1
        assert fetched.execution_mode == "oneshot_process"
        assert fetched.default_timeout_ms == 30000
        assert fetched.version == ""
        assert fetched.description == ""

    def test_toolchain_id_is_primary_key(self):
        AiToolchainTable.create(
            toolchain_id="dup-id", display_name="First", entrypoint="x"
        )
        with pytest.raises(IntegrityError):
            AiToolchainTable.create(
                toolchain_id="dup-id", display_name="Second", entrypoint="y"
            )

    def test_display_name_not_unique(self):
        """Spec does not require display_name uniqueness — versions may share a name."""
        AiToolchainTable.create(
            toolchain_id="tc-a", display_name="Motor AI", version="1.0", entrypoint="x"
        )
        AiToolchainTable.create(
            toolchain_id="tc-b", display_name="Motor AI", version="2.0", entrypoint="y"
        )
        assert AiToolchainTable.select().count() == 2

    def test_timestamps_auto_populated(self):
        row = AiToolchainTable.create(
            toolchain_id="ts-test",
            display_name="TS Test",
            entrypoint="z",
        )
        assert row.created_time is not None
        assert row.updated_time is not None
        assert len(row.created_time) > 0
        assert len(row.updated_time) > 0
