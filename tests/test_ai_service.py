"""Tests for AiService — toolchain registry, binding resolution, binding CRUD."""
from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.service.ai_service as ai_service_module
from model.ai_execution_table import AiExecutionTable
from model.test_config_table import TestConfigTable as ConfigTable
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable as AiBindingTable
from app.service.ai_service import AiService


def test_ai_service_shutdown_delegates_to_resident_runtime():
    resident_runtime = MagicMock()
    service = AiService(resident_runtime=resident_runtime)

    service.shutdown()

    resident_runtime.shutdown_all.assert_called_once_with()


def _recreate_legacy_ai_binding_table(db):
    db.execute_sql("DROP TABLE test_config_ai_binding_table")
    db.execute_sql(
        """
        CREATE TABLE test_config_ai_binding_table (
            binding_id INTEGER PRIMARY KEY,
            config_id INTEGER NOT NULL UNIQUE REFERENCES test_config_table(config_id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL DEFAULT 0,
            toolchain_id TEXT REFERENCES ai_toolchain_table(toolchain_id) ON DELETE SET NULL,
            timeout_ms INTEGER,
            analysis_window_offset_ms INTEGER NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            score_threshold REAL DEFAULT NULL,
            updated_time TEXT NOT NULL DEFAULT ''
        )
        """
        )


def _toolchain_payload(**overrides):
    payload = {
        "toolchain_id": "motor-ai",
        "display_name": "Motor AI",
        "version": "1.0",
        "entrypoint": "python motor_ai.py",
        "package_dir": os.getcwd(),
        "default_timeout_ms": 30000,
        "enabled": True,
        "description": "",
    }
    payload.update(overrides)
    return payload


def _analysis_segments(tmp_path):
    audio_path = tmp_path / "segment.wav"
    audio_path.write_bytes(b"\x00" * 64)
    return [{
        "segment_id": "seg-1",
        "role": "device_1",
        "audio_path": str(audio_path),
        "channels": [1],
        "duration_ms": 1000,
        "sample_rate": 48000,
        "metadata": {},
    }]


@pytest.fixture
def ai_service():
    service = AiService()
    try:
        yield service
    finally:
        if hasattr(service, "_resident_runtime"):
            service._resident_runtime.shutdown_all()


@pytest.fixture
def toolchain():
    return AiToolchainTable.create(
        toolchain_id="motor-ai",
        display_name="Motor AI",
        version="1.0",
        entrypoint="python motor_ai.py",
        package_dir=os.getcwd(),
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

    def test_list_toolchains_includes_disabled_rows(self, ai_service, toolchain):
        AiToolchainTable.create(
            toolchain_id="disabled-tc",
            display_name="Disabled",
            version="2",
            entrypoint="disabled.py",
            package_dir=os.getcwd(),
            enabled=False,
        )

        result = ai_service.list_toolchains()

        assert [row["toolchain_id"] for row in result] == ["disabled-tc", "motor-ai"]

    def test_list_excludes_disabled(self, ai_service):
        AiToolchainTable.create(
            toolchain_id="disabled-tc",
            display_name="Disabled",
            version="1",
            entrypoint="x",
            package_dir=os.getcwd(),
            enabled=False,
        )
        assert ai_service.list_enabled_toolchains() == []

    def test_get_toolchain(self, ai_service, toolchain):
        result = ai_service.get_toolchain("motor-ai")
        assert result is not None
        assert result.display_name == "Motor AI"

    def test_get_toolchain_not_found(self, ai_service):
        assert ai_service.get_toolchain("nonexistent") is None

    def test_list_toolchain_bindings_returns_config_id_and_name(
        self, ai_service, config, toolchain
    ):
        second = ConfigTable.create(config_name="Cfg2")
        AiBindingTable.create(config=config, toolchain=toolchain, enabled=True)
        AiBindingTable.create(config=second, toolchain=toolchain, enabled=False)

        result = ai_service.list_toolchain_bindings("motor-ai")

        assert result == [
            {"config_id": config.config_id, "config_name": "Cfg1"},
            {"config_id": second.config_id, "config_name": "Cfg2"},
        ]


class TestToolchainRegistryCrud:
    def test_generate_toolchain_id_uses_ascii_prefix_and_random_suffix(
        self, ai_service, monkeypatch
    ):
        monkeypatch.setattr(
            ai_service_module,
            "secrets",
            SimpleNamespace(token_hex=lambda n: "a1b2c3d4"),
            raising=False,
        )

        generated = ai_service.generate_toolchain_id("  Mötör AI ++  ")

        assert generated == "motor_ai_a1b2c3d4"
        assert len(generated) <= 33
        assert generated.isascii()

    def test_generate_toolchain_id_falls_back_for_non_ascii_display_name(
        self, ai_service, monkeypatch
    ):
        monkeypatch.setattr(
            ai_service_module,
            "secrets",
            SimpleNamespace(token_hex=lambda n: "11112222"),
            raising=False,
        )

        generated = ai_service.generate_toolchain_id("电机异音")

        assert generated == "toolchain_11112222"

    def test_generate_toolchain_id_regenerates_when_persisted_id_collides(
        self, ai_service, monkeypatch, tmp_path
    ):
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        AiToolchainTable.create(
            toolchain_id="motor_ai_aaaabbbb",
            display_name="Existing",
            version="1.0",
            entrypoint="python main.py",
            package_dir=str(package_dir),
            protocol_version=1,
            execution_mode="resident_process",
            default_timeout_ms=30000,
            enabled=True,
            description="",
        )
        suffixes = iter(["aaaabbbb", "ccccdddd"])
        monkeypatch.setattr(
            ai_service_module.secrets,
            "token_hex",
            lambda n: next(suffixes),
        )

        assert ai_service.generate_toolchain_id("Motor AI") == "motor_ai_ccccdddd"

    def test_generate_toolchain_id_from_prefix_reuses_original_prefix(
        self, ai_service, monkeypatch
    ):
        monkeypatch.setattr(
            ai_service_module.secrets,
            "token_hex",
            lambda n: "9999aaaa",
        )

        assert ai_service.generate_toolchain_id_from_prefix("motor_ai") == "motor_ai_9999aaaa"

    def test_save_generated_draft_retries_once_when_create_id_collides(
        self, ai_service, monkeypatch, tmp_path
    ):
        from peewee import IntegrityError

        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        original_create = AiToolchainTable.create
        calls = {"count": 0}

        def create_with_one_collision(**kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise IntegrityError(
                    "UNIQUE constraint failed: ai_toolchain_table.toolchain_id"
                )
            return original_create(**kwargs)

        monkeypatch.setattr(
            ai_service_module.secrets,
            "token_hex",
            lambda n: "11112222",
        )
        monkeypatch.setattr(AiToolchainTable, "create", create_with_one_collision)

        saved_id = ai_service.save_toolchain(
            None,
            _toolchain_payload(
                toolchain_id="motor_ai_aaaabbbb",
                display_name="Motor AI",
                package_dir=str(package_dir),
                generated_id_prefix="motor_ai",
            ),
        )

        assert saved_id == "motor_ai_11112222"
        assert calls["count"] == 2

    def test_save_explicit_create_id_collision_without_generated_prefix_does_not_retry(
        self, ai_service, monkeypatch, tmp_path
    ):
        from peewee import IntegrityError

        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        calls = {"count": 0}

        def always_raise_id_integrity(**_kwargs):
            calls["count"] += 1
            raise IntegrityError(
                "UNIQUE constraint failed: ai_toolchain_table.toolchain_id"
            )

        monkeypatch.setattr(AiToolchainTable, "create", always_raise_id_integrity)

        with pytest.raises(IntegrityError, match="toolchain_id"):
            ai_service.save_toolchain(
                None,
                _toolchain_payload(
                    toolchain_id="motor_ai_aaaabbbb",
                    display_name="Motor AI",
                    package_dir=str(package_dir),
                ),
            )

        assert calls["count"] == 1

    def test_save_generated_draft_does_not_retry_non_id_integrity_errors(
        self, ai_service, monkeypatch, tmp_path
    ):
        from peewee import IntegrityError

        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        calls = {"count": 0}

        def always_raise_non_id_integrity(**_kwargs):
            calls["count"] += 1
            raise IntegrityError(
                "NOT NULL constraint failed: ai_toolchain_table.display_name"
            )

        monkeypatch.setattr(AiToolchainTable, "create", always_raise_non_id_integrity)

        with pytest.raises(IntegrityError, match="display_name"):
            ai_service.save_toolchain(
                None,
                _toolchain_payload(
                    toolchain_id="motor_ai_aaaabbbb",
                    display_name="Motor AI",
                    package_dir=str(package_dir),
                    generated_id_prefix="motor_ai",
                ),
            )

        assert calls["count"] == 1

    def test_save_toolchain_rejects_missing_package_dir(self, ai_service):
        with pytest.raises(ValueError, match="package_dir"):
            ai_service.save_toolchain(
                None,
                _toolchain_payload(package_dir=""),
            )

    def test_save_toolchain_rejects_non_directory_package_dir(
        self, ai_service, tmp_path
    ):
        missing = tmp_path / "missing"

        with pytest.raises(ValueError, match="package_dir"):
            ai_service.save_toolchain(
                None,
                _toolchain_payload(package_dir=str(missing)),
            )

    def test_save_toolchain_round_trips_package_dir(self, ai_service, tmp_path):
        package_dir = tmp_path / "tool"
        package_dir.mkdir()

        saved_id = ai_service.save_toolchain(
            None,
            _toolchain_payload(
                toolchain_id="roundtrip-ai",
                display_name="Roundtrip AI",
                package_dir=str(package_dir),
            ),
        )

        row = ai_service.get_toolchain(saved_id)

        assert row is not None
        assert row.package_dir == str(package_dir)
        assert ai_service.list_toolchains()[0]["package_dir"] == str(package_dir)

    def test_save_toolchain_defaults_execution_mode_to_resident_process(
        self, ai_service
    ):
        ai_service.save_toolchain(
            None,
            _toolchain_payload(
                toolchain_id="new-ai",
                display_name="New AI",
                protocol_version=999,
            ),
        )

        row = AiToolchainTable.get_by_id("new-ai")

        assert row.display_name == "New AI"
        assert row.protocol_version == 1
        assert row.execution_mode == "resident_process"

    @pytest.mark.parametrize(
        "toolchain_id, execution_mode",
        [
            ("resident-ai", "resident_process"),
            ("oneshot-ai", "oneshot_process"),
        ],
    )
    def test_save_toolchain_accepts_supported_execution_modes(
        self, ai_service, toolchain_id, execution_mode
    ):
        ai_service.save_toolchain(
            None,
            _toolchain_payload(
                toolchain_id=toolchain_id,
                execution_mode=execution_mode,
            ),
        )

        row = AiToolchainTable.get_by_id(toolchain_id)

        assert row.protocol_version == 1
        assert row.execution_mode == execution_mode

    def test_save_toolchain_create_trims_persisted_string_fields(self, ai_service):
        ai_service.save_toolchain(
            None,
            _toolchain_payload(
                toolchain_id="  trimmed-ai  ",
                display_name="  Trimmed AI  ",
                version="  2.0  ",
                entrypoint="  python trimmed.py  ",
                description="  trimmed description  ",
            ),
        )

        row = AiToolchainTable.get_by_id("trimmed-ai")

        assert row.display_name == "Trimmed AI"
        assert row.version == "2.0"
        assert row.entrypoint == "python trimmed.py"
        assert row.description == "trimmed description"

    @pytest.mark.parametrize(
        "execution_mode", ["resident_process", "oneshot_process"]
    )
    def test_save_toolchain_updates_existing_row_preserves_supported_execution_mode(
        self, ai_service, toolchain, execution_mode
    ):
        toolchain.protocol_version = 9
        toolchain.execution_mode = "daemon"
        toolchain.save()

        ai_service.save_toolchain(
            "motor-ai",
            _toolchain_payload(
                display_name="Updated Motor AI",
                version="2.0",
                entrypoint="python motor_ai_v2.py",
                default_timeout_ms=45000,
                protocol_version=999,
                execution_mode=execution_mode,
            ),
        )

        row = AiToolchainTable.get_by_id("motor-ai")

        assert row.display_name == "Updated Motor AI"
        assert row.version == "2.0"
        assert row.entrypoint == "python motor_ai_v2.py"
        assert row.default_timeout_ms == 45000
        assert row.protocol_version == 1
        assert row.execution_mode == execution_mode

    def test_save_toolchain_update_trims_persisted_string_fields(
        self, ai_service, toolchain
    ):
        ai_service.save_toolchain(
            "motor-ai",
            _toolchain_payload(
                toolchain_id="  motor-ai  ",
                display_name="  Updated Motor AI  ",
                version="  2.1  ",
                entrypoint="  python updated.py  ",
                description="  updated description  ",
            ),
        )

        row = AiToolchainTable.get_by_id("motor-ai")

        assert row.display_name == "Updated Motor AI"
        assert row.version == "2.1"
        assert row.entrypoint == "python updated.py"
        assert row.description == "updated description"

    def test_save_toolchain_update_preserves_existing_execution_mode_when_omitted(
        self, ai_service, toolchain
    ):
        toolchain.execution_mode = "oneshot_process"
        toolchain.save()

        ai_service.save_toolchain(
            "motor-ai",
            _toolchain_payload(
                display_name="Updated Motor AI",
                version="2.0",
                entrypoint="python motor_ai_v2.py",
                default_timeout_ms=45000,
            ),
        )

        row = AiToolchainTable.get_by_id("motor-ai")

        assert row.execution_mode == "oneshot_process"

    @pytest.mark.parametrize("toolchain_id", ["", "   "])
    def test_save_toolchain_rejects_empty_toolchain_id(self, ai_service, toolchain_id):
        with pytest.raises(ValueError, match="toolchain_id"):
            ai_service.save_toolchain(
                None,
                _toolchain_payload(toolchain_id=toolchain_id),
            )

    def test_save_toolchain_rejects_duplicate_toolchain_id(self, ai_service, toolchain):
        with pytest.raises(ValueError, match="toolchain_id"):
            ai_service.save_toolchain(
                None,
                _toolchain_payload(toolchain_id=" motor-ai "),
            )

    def test_save_toolchain_rejects_unsupported_execution_mode(self, ai_service):
        with pytest.raises(ValueError, match="execution_mode"):
            ai_service.save_toolchain(
                None,
                _toolchain_payload(execution_mode="daemon"),
            )

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [("display_name", ""), ("display_name", "   "), ("entrypoint", ""), ("entrypoint", "   ")],
    )
    def test_save_toolchain_rejects_empty_required_text_fields(
        self, ai_service, field_name, value
    ):
        with pytest.raises(ValueError, match=field_name):
            ai_service.save_toolchain(
                None,
                _toolchain_payload(**{field_name: value}),
            )

    @pytest.mark.parametrize("timeout_ms", [0, -1])
    def test_save_toolchain_rejects_non_positive_default_timeout_ms(
        self, ai_service, timeout_ms
    ):
        with pytest.raises(ValueError, match="default_timeout_ms"):
            ai_service.save_toolchain(
                None,
                _toolchain_payload(default_timeout_ms=timeout_ms),
            )

    def test_save_toolchain_rename_rewrites_all_binding_rows_but_not_history(
        self, ai_service, config, toolchain
    ):
        toolchain.created_time = "2024-01-01 00:00:00"
        toolchain.updated_time = "2024-01-01 00:00:01"
        toolchain.save()
        original_created_time = toolchain.created_time
        second = ConfigTable.create(config_name="Cfg2")
        first_binding = AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            timeout_ms=1234,
            params_json='{"a": 1}',
            updated_time="binding-before-1",
        )
        second_binding = AiBindingTable.create(
            config=second,
            toolchain=toolchain,
            enabled=False,
            timeout_ms=4321,
            params_json='{"b": 2}',
            updated_time="binding-before-2",
        )
        AiExecutionTable.create(
            request_id="req-1",
            test_run_id="run-1",
            config_id=config.config_id,
            toolchain_id="motor-ai",
            module_status="success",
            is_current_effective=True,
        )

        ai_service.save_toolchain(
            "motor-ai",
            _toolchain_payload(
                toolchain_id="motor-ai-v2",
                version="2.0",
                entrypoint="python motor_ai_v2.py",
                default_timeout_ms=45000,
                description="updated",
            ),
        )

        renamed_toolchain = AiToolchainTable.get_by_id("motor-ai-v2")
        assert renamed_toolchain.created_time == original_created_time
        assert renamed_toolchain.updated_time != "2024-01-01 00:00:01"
        rebound_first = AiBindingTable.get_by_id(first_binding.binding_id)
        rebound_second = AiBindingTable.get_by_id(second_binding.binding_id)
        assert rebound_first.binding_id == first_binding.binding_id
        assert rebound_first.config_id == config.config_id
        assert rebound_first.toolchain_id == "motor-ai-v2"
        assert rebound_first.timeout_ms == 1234
        assert rebound_first.params_json == '{"a": 1}'
        assert rebound_first.updated_time != "binding-before-1"
        assert rebound_second.binding_id == second_binding.binding_id
        assert rebound_second.config_id == second.config_id
        assert rebound_second.toolchain_id == "motor-ai-v2"
        assert rebound_second.timeout_ms == 4321
        assert rebound_second.params_json == '{"b": 2}'
        assert rebound_second.updated_time != "binding-before-2"
        assert AiExecutionTable.get_by_id("req-1").toolchain_id == "motor-ai"

    def test_save_toolchain_rename_rolls_back_when_binding_rewrite_fails(
        self, ai_service, config, toolchain, monkeypatch
    ):
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            timeout_ms=1234,
            params_json='{"a": 1}',
        )

        def _raise_after_partial_rewrite(old_id, new_id):
            (
                AiBindingTable.update(toolchain=new_id)
                .where(AiBindingTable.toolchain == old_id)
                .execute()
            )
            raise RuntimeError("boom during rename")

        monkeypatch.setattr(
            ai_service,
            "_rewrite_toolchain_binding_ids",
            _raise_after_partial_rewrite,
            raising=False,
        )

        with pytest.raises(RuntimeError, match="boom during rename"):
            ai_service.save_toolchain(
                "motor-ai",
                _toolchain_payload(toolchain_id="motor-ai-v2"),
            )

        assert AiToolchainTable.get_by_id("motor-ai").display_name == "Motor AI"
        assert ai_service.get_toolchain("motor-ai-v2") is None
        binding = AiBindingTable.get(AiBindingTable.config == config.config_id)
        assert binding.toolchain_id == "motor-ai"

    def test_delete_toolchain_clears_affected_bindings_before_deleting_row(
        self, ai_service, config, toolchain
    ):
        second = ConfigTable.create(config_name="Cfg2")
        first_binding = AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            timeout_ms=1234,
            params_json='{"a": 1}',
            updated_time="delete-before-1",
        )
        second_binding = AiBindingTable.create(
            config=second,
            toolchain=toolchain,
            enabled=False,
            timeout_ms=4321,
            params_json='{"b": 2}',
            updated_time="delete-before-2",
        )
        AiExecutionTable.create(
            request_id="req-2",
            test_run_id="run-2",
            config_id=config.config_id,
            toolchain_id="motor-ai",
            module_status="success",
            is_current_effective=True,
        )

        ai_service.delete_toolchain("motor-ai")

        assert ai_service.get_toolchain("motor-ai") is None
        cleared_first = AiBindingTable.get_by_id(first_binding.binding_id)
        cleared_second = AiBindingTable.get_by_id(second_binding.binding_id)
        assert cleared_first.binding_id == first_binding.binding_id
        assert cleared_first.config_id == config.config_id
        assert cleared_first.enabled is False
        assert cleared_first.toolchain_id is None
        assert cleared_first.timeout_ms == 1234
        assert cleared_first.params_json == '{"a": 1}'
        assert cleared_first.updated_time != "delete-before-1"
        assert cleared_second.binding_id == second_binding.binding_id
        assert cleared_second.config_id == second.config_id
        assert cleared_second.enabled is False
        assert cleared_second.toolchain_id is None
        assert cleared_second.timeout_ms == 4321
        assert cleared_second.params_json == '{"b": 2}'
        assert cleared_second.updated_time != "delete-before-2"
        assert AiExecutionTable.get_by_id("req-2").toolchain_id == "motor-ai"

    def test_delete_toolchain_rolls_back_when_binding_clear_fails(
        self, ai_service, config, toolchain, monkeypatch
    ):
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            timeout_ms=1234,
            params_json='{"a": 1}',
        )

        def _raise_after_partial_clear(toolchain_id):
            (
                AiBindingTable.update(enabled=False, toolchain=None)
                .where(AiBindingTable.toolchain == toolchain_id)
                .execute()
            )
            raise RuntimeError("boom during clear")

        monkeypatch.setattr(
            ai_service,
            "_clear_toolchain_bindings",
            _raise_after_partial_clear,
            raising=False,
        )

        with pytest.raises(RuntimeError, match="boom during clear"):
            ai_service.delete_toolchain("motor-ai")

        assert ai_service.get_toolchain("motor-ai") is not None
        binding = AiBindingTable.get(AiBindingTable.config == config.config_id)
        assert binding.enabled is True
        assert binding.toolchain_id == "motor-ai"

    def test_delete_toolchain_rolls_back_when_row_delete_fails(
        self, ai_service, config, toolchain, monkeypatch
    ):
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            timeout_ms=1234,
            params_json='{"a": 1}',
        )

        def _raise_before_delete(toolchain_id):
            raise RuntimeError("boom during delete")

        monkeypatch.setattr(
            ai_service,
            "_delete_toolchain_row",
            _raise_before_delete,
            raising=False,
        )

        with pytest.raises(RuntimeError, match="boom during delete"):
            ai_service.delete_toolchain("motor-ai")

        assert ai_service.get_toolchain("motor-ai") is not None
        binding = AiBindingTable.get(AiBindingTable.config == config.config_id)
        assert binding.enabled is True
        assert binding.toolchain_id == "motor-ai"

    def test_save_toolchain_rejects_mutation_while_resident_request_in_flight(
        self, ai_service, toolchain, monkeypatch
    ):
        toolchain.execution_mode = "resident_process"
        toolchain.save()

        @contextmanager
        def _raise_busy(_toolchain_id):
            raise ValueError("Resident toolchain is busy")
            yield

        monkeypatch.setattr(
            ai_service._resident_runtime,
            "hold_mutation_lease",
            _raise_busy,
        )

        with pytest.raises(ValueError, match="busy"):
            ai_service.save_toolchain(
                "motor-ai",
                _toolchain_payload(
                    display_name="Busy Update",
                    execution_mode="resident_process",
                ),
            )

        assert AiToolchainTable.get_by_id("motor-ai").display_name == "Motor AI"

    def test_save_toolchain_update_holds_resident_lease_through_invalidate(
        self, ai_service, toolchain, monkeypatch
    ):
        toolchain.execution_mode = "resident_process"
        toolchain.save()
        calls = []
        lease_state = {"held": False}

        @contextmanager
        def _hold(toolchain_id):
            calls.append(("lease_enter", toolchain_id))
            lease_state["held"] = True
            try:
                yield
            finally:
                lease_state["held"] = False
                calls.append(("lease_exit", toolchain_id))

        monkeypatch.setattr(ai_service._resident_runtime, "hold_mutation_lease", _hold)

        def _invalidate(toolchain_id):
            row = AiToolchainTable.get_by_id("motor-ai")
            assert row.entrypoint == "python motor_ai_v2.py"
            assert lease_state["held"] is True
            calls.append(("invalidate", toolchain_id))

        monkeypatch.setattr(ai_service._resident_runtime, "invalidate", _invalidate)

        ai_service.save_toolchain(
            "motor-ai",
            _toolchain_payload(
                entrypoint="python motor_ai_v2.py",
                execution_mode="resident_process",
            ),
        )

        assert calls == [
            ("lease_enter", "motor-ai"),
            ("invalidate", "motor-ai"),
            ("lease_exit", "motor-ai"),
        ]

    def test_save_toolchain_rename_holds_old_resident_lease_through_invalidate(
        self, ai_service, config, toolchain, monkeypatch
    ):
        toolchain.execution_mode = "resident_process"
        toolchain.save()
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            timeout_ms=1234,
            params_json="{}",
        )
        calls = []
        lease_state = {"held": False}

        @contextmanager
        def _hold(toolchain_id):
            calls.append(("lease_enter", toolchain_id))
            lease_state["held"] = True
            try:
                yield
            finally:
                lease_state["held"] = False
                calls.append(("lease_exit", toolchain_id))

        monkeypatch.setattr(ai_service._resident_runtime, "hold_mutation_lease", _hold)

        def _invalidate(toolchain_id):
            assert ai_service.get_toolchain("motor-ai") is None
            assert ai_service.get_toolchain("motor-ai-v2") is not None
            rebound = AiBindingTable.get(AiBindingTable.config == config.config_id)
            assert rebound.toolchain_id == "motor-ai-v2"
            assert lease_state["held"] is True
            calls.append(("invalidate", toolchain_id))

        monkeypatch.setattr(ai_service._resident_runtime, "invalidate", _invalidate)

        result = ai_service.save_toolchain(
            "motor-ai",
            _toolchain_payload(
                toolchain_id="motor-ai-v2",
                execution_mode="resident_process",
            ),
        )

        assert result == "motor-ai-v2"
        assert calls == [
            ("lease_enter", "motor-ai"),
            ("invalidate", "motor-ai"),
            ("lease_exit", "motor-ai"),
        ]

    def test_save_toolchain_rename_propagates_busy_error_without_mutation(
        self, ai_service, config, toolchain, monkeypatch
    ):
        toolchain.execution_mode = "resident_process"
        toolchain.save()
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            timeout_ms=1234,
            params_json="{}",
        )
        invalidations = []

        @contextmanager
        def _raise_busy(_toolchain_id):
            raise ValueError("Resident toolchain is busy")
            yield

        monkeypatch.setattr(
            ai_service._resident_runtime,
            "hold_mutation_lease",
            _raise_busy,
        )
        monkeypatch.setattr(
            ai_service._resident_runtime,
            "invalidate",
            lambda toolchain_id: invalidations.append(toolchain_id),
        )

        with pytest.raises(ValueError, match="busy"):
            ai_service.save_toolchain(
                "motor-ai",
                _toolchain_payload(
                    toolchain_id="motor-ai-v2",
                    execution_mode="resident_process",
                ),
            )

        assert ai_service.get_toolchain("motor-ai") is not None
        assert ai_service.get_toolchain("motor-ai-v2") is None
        rebound = AiBindingTable.get(AiBindingTable.config == config.config_id)
        assert rebound.toolchain_id == "motor-ai"
        assert invalidations == []

    def test_delete_toolchain_holds_resident_lease_through_invalidate_after_success(
        self, ai_service, config, toolchain, monkeypatch
    ):
        toolchain.execution_mode = "resident_process"
        toolchain.save()
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            timeout_ms=1234,
            params_json="{}",
        )
        calls = []
        lease_state = {"held": False}

        @contextmanager
        def _hold(toolchain_id):
            calls.append(("lease_enter", toolchain_id))
            lease_state["held"] = True
            try:
                yield
            finally:
                lease_state["held"] = False
                calls.append(("lease_exit", toolchain_id))

        monkeypatch.setattr(ai_service._resident_runtime, "hold_mutation_lease", _hold)

        def _invalidate(toolchain_id):
            assert ai_service.get_toolchain("motor-ai") is None
            binding = AiBindingTable.get(AiBindingTable.config == config.config_id)
            assert binding.toolchain_id is None
            assert lease_state["held"] is True
            calls.append(("invalidate", toolchain_id))

        monkeypatch.setattr(ai_service._resident_runtime, "invalidate", _invalidate)

        ai_service.delete_toolchain("motor-ai")

        assert calls == [
            ("lease_enter", "motor-ai"),
            ("invalidate", "motor-ai"),
            ("lease_exit", "motor-ai"),
        ]

    def test_delete_toolchain_busy_error_does_not_mutate_or_invalidate(
        self, ai_service, config, toolchain, monkeypatch
    ):
        toolchain.execution_mode = "resident_process"
        toolchain.save()
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            timeout_ms=1234,
            params_json="{}",
        )
        invalidations = []

        @contextmanager
        def _raise_busy(_toolchain_id):
            raise ValueError("Resident toolchain is busy")
            yield

        monkeypatch.setattr(
            ai_service._resident_runtime,
            "hold_mutation_lease",
            _raise_busy,
        )
        monkeypatch.setattr(
            ai_service._resident_runtime,
            "invalidate",
            lambda toolchain_id: invalidations.append(toolchain_id),
        )

        with pytest.raises(ValueError, match="busy"):
            ai_service.delete_toolchain("motor-ai")

        assert ai_service.get_toolchain("motor-ai") is not None
        binding = AiBindingTable.get(AiBindingTable.config == config.config_id)
        assert binding.enabled is True
        assert binding.toolchain_id == "motor-ai"
        assert invalidations == []

    def test_delete_toolchain_does_not_invalidate_resident_cache_when_delete_fails(
        self, ai_service, config, toolchain, monkeypatch
    ):
        toolchain.execution_mode = "resident_process"
        toolchain.save()
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            timeout_ms=1234,
            params_json="{}",
        )
        calls = []

        @contextmanager
        def _hold(toolchain_id):
            calls.append(("lease_enter", toolchain_id))
            try:
                yield
            finally:
                calls.append(("lease_exit", toolchain_id))

        monkeypatch.setattr(ai_service._resident_runtime, "hold_mutation_lease", _hold)
        monkeypatch.setattr(
            ai_service._resident_runtime,
            "invalidate",
            lambda toolchain_id: calls.append(("invalidate", toolchain_id)),
        )

        def _raise_before_delete(_toolchain_id):
            raise RuntimeError("boom during delete")

        monkeypatch.setattr(
            ai_service,
            "_delete_toolchain_row",
            _raise_before_delete,
            raising=False,
        )

        with pytest.raises(RuntimeError, match="boom during delete"):
            ai_service.delete_toolchain("motor-ai")

        assert ai_service.get_toolchain("motor-ai") is not None
        binding = AiBindingTable.get(AiBindingTable.config == config.config_id)
        assert binding.enabled is True
        assert binding.toolchain_id == "motor-ai"
        assert calls == [
            ("lease_enter", "motor-ai"),
            ("lease_exit", "motor-ai"),
        ]


class TestRunAiAnalysisRouting:
    def test_execute_toolchain_runs_with_package_dir_cwd(
        self, ai_service, tmp_path, monkeypatch
    ):
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        captured = {}

        def _run(cmd, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", _run)

        ai_service.execute_toolchain(
            entrypoint="python main.py",
            package_dir=str(package_dir),
            request_json_path="request.json",
            response_json_path="response.json",
            artifacts_dir="artifacts",
            timeout_ms=30000,
        )

        assert captured["cwd"] == str(package_dir)

    def test_run_ai_analysis_routes_resident_process_through_runtime(
        self, ai_service, config, toolchain, tmp_path, monkeypatch
    ):
        toolchain.execution_mode = "resident_process"
        toolchain.save()
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            params_json='{"resident": true}',
        )
        segments = _analysis_segments(tmp_path)
        resident_calls = []

        def _analyze(**kwargs):
            resident_calls.append(kwargs)
            return {
                "module_status": "success",
                "started_at": "2026-04-09T00:00:00+08:00",
                "finished_at": "2026-04-09T00:00:01+08:00",
                "duration_ms": 15,
                "error_summary": None,
                "response_payload": {
                    "protocol_version": 1,
                    "request_id": kwargs["request_id"],
                    "toolchain_name": "Resident Toolchain",
                    "toolchain_version": "1.0",
                    "ai_result": {
                        "ai_label": "PASS",
                        "artifacts": [],
                        "extra": {"transport": "resident"},
                    },
                },
                "stdout_excerpt": None,
                "stderr_excerpt": "resident stderr",
                "process_instance_id": 7,
            }

        monkeypatch.setattr(ai_service._resident_runtime, "analyze", _analyze)
        monkeypatch.setattr(
            ai_service,
            "execute_toolchain",
            lambda **kwargs: pytest.fail("oneshot path should not run"),
        )

        result = ai_service.run_ai_analysis(
            config_id=config.config_id,
            test_run_id="run_resident_route",
            segments=segments,
            context={"config_id": config.config_id},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )

        assert result["module_status"] == "success"
        assert resident_calls[0]["toolchain_id"] == "motor-ai"
        assert resident_calls[0]["request_id"] == result["request_id"]
        assert resident_calls[0]["request_payload"]["toolchain_id"] == "motor-ai"
        assert resident_calls[0]["package_dir"] == toolchain.package_dir
        assert resident_calls[0]["artifacts_dir"] == result["artifacts_dir"]

    def test_run_ai_analysis_routes_oneshot_process_through_execute_toolchain(
        self, ai_service, config, toolchain, tmp_path, monkeypatch
    ):
        toolchain.execution_mode = "oneshot_process"
        toolchain.save()
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            params_json="{}",
        )
        segments = _analysis_segments(tmp_path)
        execute_calls = []

        monkeypatch.setattr(
            ai_service._resident_runtime,
            "analyze",
            lambda **kwargs: pytest.fail("resident runtime should not run"),
        )

        def _execute(
            *,
            entrypoint: str,
            package_dir: str,
            request_json_path: str,
            response_json_path: str,
            artifacts_dir: str,
            timeout_ms: int,
        ):
            execute_calls.append(
                {
                    "entrypoint": entrypoint,
                    "package_dir": package_dir,
                    "request_json_path": request_json_path,
                    "response_json_path": response_json_path,
                    "artifacts_dir": artifacts_dir,
                    "timeout_ms": timeout_ms,
                }
            )
            with open(request_json_path, "r", encoding="utf-8") as f:
                request_payload = json.load(f)
            response_payload = {
                "protocol_version": 1,
                "request_id": request_payload["request_id"],
                "toolchain_name": "Oneshot Toolchain",
                "toolchain_version": "1.0",
                "ai_result": {
                    "ai_label": "PASS",
                    "artifacts": [],
                },
            }
            with open(response_json_path, "w", encoding="utf-8") as f:
                json.dump(response_payload, f, ensure_ascii=False, indent=2)
            return {
                "started_at": "2026-04-09T00:00:00+08:00",
                "finished_at": "2026-04-09T00:00:01+08:00",
                "duration_ms": 12,
                "exit_code": 0,
                "stdout_excerpt": "",
                "stderr_excerpt": "",
            }

        monkeypatch.setattr(ai_service, "execute_toolchain", _execute)

        result = ai_service.run_ai_analysis(
            config_id=config.config_id,
            test_run_id="run_oneshot_route",
            segments=segments,
            context={"config_id": config.config_id},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )

        assert result["module_status"] == "success"
        assert execute_calls[0]["package_dir"] == toolchain.package_dir
        assert execute_calls[0]["request_json_path"] == result["request_json_path"]
        assert execute_calls[0]["response_json_path"] == result["response_json_path"]

    def test_run_ai_analysis_resident_invalid_logical_payload_does_not_persist_response_json(
        self, ai_service, config, toolchain, tmp_path, monkeypatch
    ):
        toolchain.execution_mode = "resident_process"
        toolchain.save()
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            params_json="{}",
        )
        segments = _analysis_segments(tmp_path)

        def _analyze(**kwargs):
            return {
                "module_status": "success",
                "started_at": "2026-04-09T00:00:00+08:00",
                "finished_at": "2026-04-09T00:00:01+08:00",
                "duration_ms": 15,
                "error_summary": None,
                "response_payload": {
                    "protocol_version": 1,
                    "request_id": kwargs["request_id"],
                    "toolchain_name": "Resident Toolchain",
                    "toolchain_version": "1.0",
                    "ai_result": {
                        "ai_label": "PASS",
                        "score": "bad-score",
                        "artifacts": [],
                    },
                },
                "stdout_excerpt": None,
                "stderr_excerpt": "resident stderr",
                "process_instance_id": 7,
            }

        monkeypatch.setattr(ai_service._resident_runtime, "analyze", _analyze)
        monkeypatch.setattr(
            ai_service,
            "execute_toolchain",
            lambda **kwargs: pytest.fail("oneshot path should not run"),
        )

        result = ai_service.run_ai_analysis(
            config_id=config.config_id,
            test_run_id="run_resident_invalid_payload",
            segments=segments,
            context={"config_id": config.config_id},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )

        assert result["module_status"] == "invalid_output"
        assert result["error_summary"] == "ai_result.score must be numeric (not bool)"
        assert os.path.isfile(result["request_json_path"])
        assert os.path.exists(result["response_json_path"]) is False

    def test_run_ai_analysis_resident_response_json_persistence_failure_returns_failed_envelope_and_saves_execution(
        self, ai_service, config, toolchain, tmp_path, monkeypatch
    ):
        toolchain.execution_mode = "resident_process"
        toolchain.save()
        AiBindingTable.create(
            config=config,
            toolchain=toolchain,
            enabled=True,
            params_json="{}",
        )
        AiExecutionTable.create(
            request_id="req_previous_success",
            test_run_id="run_resident_persist_fail",
            config_id=config.config_id,
            toolchain_id=toolchain.toolchain_id,
            module_status="success",
            error_summary=None,
            is_current_effective=True,
        )
        segments = _analysis_segments(tmp_path)
        fixed_request_id = "req_resident_persist_fail"
        ai_runs_dir = tmp_path / "ai_runs"
        expected_response_path = os.path.join(
            str(ai_runs_dir),
            fixed_request_id,
            "response.json",
        )
        real_json_dump = json.dump

        def _analyze(**kwargs):
            return {
                "module_status": "success",
                "started_at": "2026-04-09T00:00:00+08:00",
                "finished_at": "2026-04-09T00:00:01+08:00",
                "duration_ms": 15,
                "error_summary": None,
                "response_payload": {
                    "protocol_version": 1,
                    "request_id": kwargs["request_id"],
                    "toolchain_name": "Resident Toolchain",
                    "toolchain_version": "1.0",
                    "ai_result": {
                        "ai_label": "PASS",
                        "artifacts": [],
                    },
                },
                "stdout_excerpt": None,
                "stderr_excerpt": "resident stderr",
                "process_instance_id": 7,
            }

        def _dump(data, fp, *args, **kwargs):
            if getattr(fp, "name", None) == expected_response_path:
                fp.write("{")
                fp.flush()
                raise OSError("disk full while writing response.json")
            return real_json_dump(data, fp, *args, **kwargs)

        monkeypatch.setattr(ai_service_module, "_gen_request_id", lambda: fixed_request_id)
        monkeypatch.setattr(ai_service._resident_runtime, "analyze", _analyze)
        monkeypatch.setattr(
            ai_service,
            "execute_toolchain",
            lambda **kwargs: pytest.fail("oneshot path should not run"),
        )
        monkeypatch.setattr(ai_service_module.json, "dump", _dump)

        result = ai_service.run_ai_analysis(
            config_id=config.config_id,
            test_run_id="run_resident_persist_fail",
            segments=segments,
            context={"config_id": config.config_id},
            ai_runs_dir=str(ai_runs_dir),
        )

        assert result["request_id"] == fixed_request_id
        assert result["module_status"] == "process_error"
        assert result["error_summary"] == (
            "Failed to persist response.json: "
            "disk full while writing response.json"
        )
        assert os.path.isfile(result["request_json_path"])
        assert os.path.exists(result["response_json_path"]) is False

        saved = AiExecutionTable.get_by_id(fixed_request_id)
        assert saved.module_status == "process_error"
        assert saved.error_summary == result["error_summary"]
        assert saved.is_current_effective is False

        previous = AiExecutionTable.get_by_id("req_previous_success")
        assert previous.is_current_effective is False

        vm = ai_service.get_ai_view_model("run_resident_persist_fail")
        assert vm["display_status"] == "process_error"
        assert vm["error_summary"] == result["error_summary"]


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

    def test_resolve_binding_does_not_include_score_threshold(
        self, ai_service, config, toolchain
    ):
        AiBindingTable.create(
            config=config, toolchain=toolchain, enabled=True
        )

        binding = ai_service.resolve_binding(config.config_id)
        assert "score_threshold" not in binding

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

    def test_save_binding_creates_row_when_legacy_binding_offset_column_is_not_null(
        self, ai_service, config, toolchain
    ):
        db = AiBindingTable._meta.database
        _recreate_legacy_ai_binding_table(db)

        binding_id = ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "params_json": '{"k": "v"}',
        })

        row = db.execute_sql(
            """
            SELECT enabled, toolchain_id, analysis_window_offset_ms, params_json
            FROM test_config_ai_binding_table
            WHERE binding_id = ?
            """,
            (binding_id,),
        ).fetchone()

        assert row == (1, "motor-ai", 0, '{"k": "v"}')

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

    def test_save_binding_updates_existing_row_when_legacy_binding_offset_column_is_not_null(
        self, ai_service, config, toolchain
    ):
        db = AiBindingTable._meta.database
        _recreate_legacy_ai_binding_table(db)
        db.execute_sql(
            """
            INSERT INTO test_config_ai_binding_table (
                binding_id,
                config_id,
                enabled,
                toolchain_id,
                timeout_ms,
                analysis_window_offset_ms,
                params_json,
                score_threshold,
                updated_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, config.config_id, 0, None, None, 250, "{}", None, ""),
        )

        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "timeout_ms": 5000,
        })

        binding = ai_service.resolve_binding(config.config_id)
        legacy_offset = db.execute_sql(
            """
            SELECT analysis_window_offset_ms
            FROM test_config_ai_binding_table
            WHERE config_id = ?
            """,
            (config.config_id,),
        ).fetchone()

        assert binding["enabled"] is True
        assert binding["timeout_ms"] == 5000
        assert "analysis_window_offset_ms" not in binding
        assert legacy_offset == (250,)

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

    def test_save_binding_legacy_compatibility_does_not_mask_unrelated_insert_failures(
        self, ai_service, config, toolchain, monkeypatch
    ):
        db = AiBindingTable._meta.database
        _recreate_legacy_ai_binding_table(db)
        original_execute_sql = db.execute_sql

        def _raising_execute_sql(sql, params=None):
            normalized_sql = str(sql).replace('"', "").replace("'", "").lower()
            if "insert into test_config_ai_binding_table" in normalized_sql:
                raise RuntimeError("different failure")
            return original_execute_sql(sql, params)

        monkeypatch.setattr(db, "execute_sql", _raising_execute_sql)

        with pytest.raises(RuntimeError, match="different failure"):
            ai_service.save_binding(config.config_id, {
                "enabled": True,
                "toolchain_id": "motor-ai",
            })


    def test_save_binding_rejects_removed_score_threshold(
        self, ai_service, config
    ):
        with pytest.raises(ValueError, match="score_threshold"):
            ai_service.save_binding(config.config_id, {
                "enabled": True,
                "score_threshold": 0.5,
            })
