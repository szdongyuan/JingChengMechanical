"""Tests for AiService preflight checks."""
from __future__ import annotations

import os
import tempfile

import pytest

from model.test_config_table import TestConfigTable
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable
from app.service.ai_service import AiService


@pytest.fixture
def ai_service():
    service = AiService()
    try:
        yield service
    finally:
        service.shutdown()


@pytest.fixture
def full_setup(tmp_path):
    cfg = TestConfigTable.create(config_name="PF Test")
    tc = AiToolchainTable.create(
        toolchain_id="pf-tc",
        display_name="TC PF",
        version="1",
        entrypoint="echo",
        package_dir=str(tmp_path),
    )
    TestConfigAiBindingTable.create(
        config=cfg, toolchain=tc, enabled=True, params_json="{}"
    )
    return cfg, tc


class TestPreflightCheck:
    def test_not_configured_no_binding(self, ai_service):
        cfg = TestConfigTable.create(config_name="NoBind")
        result = ai_service.preflight_check(cfg.config_id, [{"audio_path": "/tmp/a.wav"}])
        assert result["status"] == "not_configured"

    def test_not_enabled(self, ai_service):
        cfg = TestConfigTable.create(config_name="Disabled")
        tc = AiToolchainTable.create(
            toolchain_id="dis-tc",
            display_name="D",
            version="1",
            entrypoint="e",
            package_dir=os.getcwd(),
        )
        TestConfigAiBindingTable.create(config=cfg, toolchain=tc, enabled=False)
        result = ai_service.preflight_check(cfg.config_id, [{"audio_path": "/tmp/a.wav"}])
        assert result["status"] == "not_enabled"

    def test_not_configured_null_toolchain(self, ai_service):
        """Binding exists but toolchain is NULL."""
        cfg = TestConfigTable.create(config_name="NullTC")
        TestConfigAiBindingTable.create(config=cfg, toolchain=None, enabled=True)
        result = ai_service.preflight_check(cfg.config_id, [{"audio_path": "/tmp/a.wav"}])
        assert result["status"] == "not_configured"

    def test_not_configured_toolchain_disabled(self, ai_service):
        cfg = TestConfigTable.create(config_name="TCOff")
        tc = AiToolchainTable.create(
            toolchain_id="off-tc", display_name="Off", version="1",
            entrypoint="e", package_dir=os.getcwd(), enabled=False,
        )
        TestConfigAiBindingTable.create(config=cfg, toolchain=tc, enabled=True)
        result = ai_service.preflight_check(cfg.config_id, [{"audio_path": "/tmp/a.wav"}])
        assert result["status"] == "not_configured"

    def test_preflight_error_when_package_dir_is_missing(self, ai_service, tmp_path):
        cfg = TestConfigTable.create(config_name="MissingPackageDir")
        missing_dir = tmp_path / "missing-package"
        tc = AiToolchainTable.create(
            toolchain_id="missing-pkg-tc",
            display_name="Missing Package TC",
            version="1",
            entrypoint="e",
            package_dir=str(missing_dir),
        )
        TestConfigAiBindingTable.create(config=cfg, toolchain=tc, enabled=True)

        result = ai_service.preflight_check(cfg.config_id, [{"audio_path": "/tmp/a.wav"}])

        assert result["status"] == "preflight_error"
        assert "package_dir" in result["error"]

    def test_preflight_error_invalid_params_json(self, ai_service):
        cfg = TestConfigTable.create(config_name="BadJSON")
        tc = AiToolchainTable.create(
            toolchain_id="bj-tc",
            display_name="BJ",
            version="1",
            entrypoint="e",
            package_dir=os.getcwd(),
            params_json="not json",
        )
        TestConfigAiBindingTable.create(config=cfg, toolchain=tc, enabled=True)
        result = ai_service.preflight_check(cfg.config_id, [{"audio_path": "/tmp/a.wav"}])
        assert result["status"] == "preflight_error"

    def test_preflight_error_empty_segments(self, ai_service, full_setup):
        cfg, _ = full_setup
        result = ai_service.preflight_check(cfg.config_id, [])
        assert result["status"] == "preflight_error"

    def test_preflight_error_missing_audio(self, ai_service, full_setup):
        cfg, _ = full_setup
        result = ai_service.preflight_check(
            cfg.config_id, [{"audio_path": "/nonexistent/path.wav"}]
        )
        assert result["status"] == "preflight_error"

    def test_preflight_ok(self, ai_service, full_setup):
        cfg, _ = full_setup
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        try:
            result = ai_service.preflight_check(
                cfg.config_id, [{"audio_path": tmp_path}]
            )
            assert result["status"] == "ok"
            assert "binding" in result
            assert "toolchain" in result
        finally:
            os.unlink(tmp_path)
