"""Integration test for AiService.run_ai_analysis end-to-end."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid

import pytest

from model.test_config_table import TestConfigTable
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable
from model.ai_execution_table import AiExecutionTable
from app.service.ai_service import AiService


@pytest.fixture
def ai_service():
    return AiService()


@pytest.fixture
def fake_audio_file():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"\x00" * 100)
        path = f.name
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def fake_toolchain_script(tmp_path):
    """Python script that acts as a fake AI toolchain."""
    script = tmp_path / "fake_toolchain.py"
    script.write_text(
        '''
import json, sys

args = {}
i = 1
while i < len(sys.argv):
    if sys.argv[i] == "--request":
        args["request"] = sys.argv[i+1]; i += 2
    elif sys.argv[i] == "--response":
        args["response"] = sys.argv[i+1]; i += 2
    elif sys.argv[i] == "--artifacts-dir":
        args["artifacts_dir"] = sys.argv[i+1]; i += 2
    else:
        i += 1

with open(args["request"], "r", encoding="utf-8") as f:
    req = json.load(f)

response = {
    "protocol_version": 1,
    "request_id": req["request_id"],
    "toolchain_name": "Fake Motor AI",
    "toolchain_version": "0.1.0",
    "ai_result": {
        "ai_label": "PASS",
        "score": 0.95,
        "summary": "All good",
        "confidence": 0.9,
        "artifacts": [],
        "extra": {"test_key": "test_value"},
    },
}

with open(args["response"], "w", encoding="utf-8") as f:
    json.dump(response, f, ensure_ascii=False, indent=2)
''',
        encoding="utf-8",
    )
    return str(script)


def _write_threshold_toolchain_script(tmp_path, *, extra_payload: dict) -> str:
    script = tmp_path / f"threshold_toolchain_{uuid.uuid4().hex[:8]}.py"
    script.write_text(
        f'''
import json, sys

args = {{}}
i = 1
while i < len(sys.argv):
    if sys.argv[i] == "--request":
        args["request"] = sys.argv[i+1]; i += 2
    elif sys.argv[i] == "--response":
        args["response"] = sys.argv[i+1]; i += 2
    elif sys.argv[i] == "--artifacts-dir":
        args["artifacts_dir"] = sys.argv[i+1]; i += 2
    else:
        i += 1

with open(args["request"], "r", encoding="utf-8") as f:
    req = json.load(f)

response = {{
    "protocol_version": 1,
    "request_id": req["request_id"],
    "toolchain_name": "Threshold Toolchain",
    "toolchain_version": "0.1.0",
    "ai_result": {{
        "ai_label": "PASS",
        "score": 0.95,
        "summary": "Threshold test",
        "confidence": 0.9,
        "artifacts": [],
        "extra": json.loads(r"""{json.dumps(extra_payload, ensure_ascii=False)}"""),
    }},
}}

with open(args["response"], "w", encoding="utf-8") as f:
    json.dump(response, f, ensure_ascii=False, indent=2)
''',
        encoding="utf-8",
    )
    return str(script)


def _run_threshold_analysis(
    ai_service,
    fake_audio_file: str,
    tmp_path,
    *,
    response_extra: dict,
    request_params: dict,
) -> dict:
    cfg = TestConfigTable.create(config_name=f"Threshold Test {uuid.uuid4().hex[:8]}")
    tc = AiToolchainTable.create(
        toolchain_id=f"threshold-tc-{uuid.uuid4().hex[:8]}",
        display_name="Threshold TC",
        version="0.1.0",
        entrypoint=f"{sys.executable} {_write_threshold_toolchain_script(tmp_path, extra_payload=response_extra)}",
        default_timeout_ms=10000,
    )
    TestConfigAiBindingTable.create(
        config=cfg,
        toolchain=tc,
        enabled=True,
        params_json=json.dumps(request_params),
    )

    segments = [{
        "segment_id": "dev_1",
        "role": "device_1",
        "audio_path": fake_audio_file,
        "channels": [1],
        "duration_ms": 5000,
        "sample_rate": 48000,
        "metadata": {},
    }]

    return ai_service.run_ai_analysis(
        config_id=cfg.config_id,
        test_run_id=f"run_threshold_{uuid.uuid4().hex[:8]}",
        segments=segments,
        context={"config_id": cfg.config_id},
        ai_runs_dir=str(tmp_path / "ai_runs"),
    )


class TestRunAiAnalysis:
    def test_success_end_to_end(
        self, ai_service, fake_audio_file, fake_toolchain_script, tmp_path
    ):
        cfg = TestConfigTable.create(config_name="E2E Test")
        tc = AiToolchainTable.create(
            toolchain_id="fake-tc",
            display_name="Fake TC",
            version="0.1.0",
            entrypoint=f"{sys.executable} {fake_toolchain_script}",
            default_timeout_ms=10000,
        )
        TestConfigAiBindingTable.create(
            config=cfg, toolchain=tc, enabled=True, params_json="{}"
        )

        segments = [{
            "segment_id": "dev_1", "role": "device_1",
            "audio_path": fake_audio_file, "channels": [1],
            "duration_ms": 5000, "sample_rate": 48000, "metadata": {},
        }]

        result = ai_service.run_ai_analysis(
            config_id=cfg.config_id,
            test_run_id="run_e2e_001",
            segments=segments,
            context={"config_id": cfg.config_id, "config_name": "E2E Test"},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )

        assert result["module_status"] == "success"
        assert result["ai_label"] == "PASS"
        assert result["score"] == 0.95
        assert result["is_current_effective"] is True

        # Verify persisted
        record = AiExecutionTable.get_by_id(result["request_id"])
        assert record.module_status == "success"
        assert record.is_current_effective is True

    def test_run_ai_analysis_writes_window_info_to_request_json(
        self, ai_service, fake_audio_file, fake_toolchain_script, tmp_path
    ):
        cfg = TestConfigTable.create(config_name="Window Request Test")
        tc = AiToolchainTable.create(
            toolchain_id="window-tc",
            display_name="Window TC",
            version="0.1.0",
            entrypoint=f"{sys.executable} {fake_toolchain_script}",
            default_timeout_ms=10000,
        )
        TestConfigAiBindingTable.create(
            config=cfg, toolchain=tc, enabled=True, params_json="{}"
        )

        segments = [{
            "segment_id": "dev_1", "role": "device_1",
            "audio_path": fake_audio_file, "channels": [1],
            "duration_ms": 400, "sample_rate": 48000, "metadata": {},
        }]
        window_info = {
            "window_overridden": True,
            "default_window_offset_ms": 0,
            "default_window_duration_ms": 1000,
            "final_window_offset_ms": 100,
            "final_window_duration_ms": 400,
        }

        result = ai_service.run_ai_analysis(
            config_id=cfg.config_id,
            test_run_id="run_window_001",
            segments=segments,
            context={"config_id": cfg.config_id, "config_name": "Window Request Test"},
            ai_runs_dir=str(tmp_path / "ai_runs"),
            window_info=window_info,
        )

        with open(result["request_json_path"], "r", encoding="utf-8") as f:
            request_payload = json.load(f)

        assert request_payload["window_info"] == window_info

    def test_run_ai_analysis_uses_response_threshold_when_present(
        self, ai_service, fake_audio_file, tmp_path
    ):
        result = _run_threshold_analysis(
            ai_service,
            fake_audio_file,
            tmp_path,
            response_extra={"decision_threshold": 0.8},
            request_params={"decision_threshold": 0.6},
        )

        assert result["module_status"] == "success"
        assert result["decision_threshold"] == pytest.approx(0.8)
        assert result["decision_threshold_source"] == "response_extra"

    def test_run_ai_analysis_falls_back_to_request_threshold_when_response_threshold_invalid(
        self, ai_service, fake_audio_file, tmp_path
    ):
        result = _run_threshold_analysis(
            ai_service,
            fake_audio_file,
            tmp_path,
            response_extra={"decision_threshold": "not-a-number"},
            request_params={"decision_threshold": 0.6},
        )

        assert result["module_status"] == "success"
        assert result["decision_threshold"] == pytest.approx(0.6)
        assert result["decision_threshold_source"] == "request_params"

    @pytest.mark.parametrize(
        ("response_extra", "request_params"),
        [
            ({"decision_threshold": 1.2}, {"decision_threshold": -0.1}),
            ({}, {"decision_threshold": "bad"}),
        ],
    )
    def test_run_ai_analysis_falls_back_to_default_point_five_when_no_valid_threshold_exists(
        self,
        ai_service,
        fake_audio_file,
        tmp_path,
        response_extra,
        request_params,
    ):
        result = _run_threshold_analysis(
            ai_service,
            fake_audio_file,
            tmp_path,
            response_extra=response_extra,
            request_params=request_params,
        )

        assert result["module_status"] == "success"
        assert result["decision_threshold"] == pytest.approx(0.5)
        assert result["decision_threshold_source"] == "default"

    def test_preflight_failure_persisted(self, ai_service, tmp_path):
        cfg = TestConfigTable.create(config_name="PF Fail")
        result = ai_service.run_ai_analysis(
            config_id=cfg.config_id,
            test_run_id="run_pf_fail",
            segments=[{"audio_path": "/fake.wav"}],
            context={},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )
        assert result["module_status"] == "not_configured"
        record = AiExecutionTable.get_by_id(result["request_id"])
        assert record.module_status == "not_configured"

    def test_replaces_current_effective(
        self, ai_service, fake_audio_file, fake_toolchain_script, tmp_path
    ):
        """Second run on same test_run_id replaces the first as current effective."""
        cfg = TestConfigTable.create(config_name="Replace Test")
        tc = AiToolchainTable.create(
            toolchain_id="replace-tc",
            display_name="Replace TC",
            version="1",
            entrypoint=f"{sys.executable} {fake_toolchain_script}",
            default_timeout_ms=10000,
        )
        TestConfigAiBindingTable.create(
            config=cfg, toolchain=tc, enabled=True, params_json="{}"
        )

        segments = [{
            "segment_id": "d1", "role": "device_1",
            "audio_path": fake_audio_file, "channels": [1],
            "duration_ms": 5000, "sample_rate": 48000, "metadata": {},
        }]

        r1 = ai_service.run_ai_analysis(
            config_id=cfg.config_id, test_run_id="run_replace",
            segments=segments, context={},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )
        assert r1["is_current_effective"] is True

        r2 = ai_service.run_ai_analysis(
            config_id=cfg.config_id, test_run_id="run_replace",
            segments=segments, context={},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )
        assert r2["is_current_effective"] is True

        # First should no longer be effective
        old = AiExecutionTable.get_by_id(r1["request_id"])
        assert old.is_current_effective is False

        # Exactly one effective per test_run
        effective_count = (
            AiExecutionTable.select()
            .where(
                AiExecutionTable.test_run_id == "run_replace",
                AiExecutionTable.is_current_effective == True,
            )
            .count()
        )
        assert effective_count == 1

    def test_view_model_after_success(
        self, ai_service, fake_audio_file, fake_toolchain_script, tmp_path
    ):
        cfg = TestConfigTable.create(config_name="VM Test")
        tc = AiToolchainTable.create(
            toolchain_id="vm-tc",
            display_name="VM TC", version="1",
            entrypoint=f"{sys.executable} {fake_toolchain_script}",
            default_timeout_ms=10000,
        )
        TestConfigAiBindingTable.create(
            config=cfg, toolchain=tc, enabled=True, params_json="{}"
        )

        segments = [{
            "segment_id": "d1", "role": "device_1",
            "audio_path": fake_audio_file, "channels": [1],
            "duration_ms": 5000, "sample_rate": 48000, "metadata": {},
        }]

        ai_service.run_ai_analysis(
            config_id=cfg.config_id, test_run_id="run_vm",
            segments=segments, context={},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )

        vm = ai_service.get_ai_view_model("run_vm")
        assert vm["has_result"] is True
        assert vm["display_status"] == "success"
        assert vm["ai_label"] == "PASS"
        assert vm["ai_score"] == 0.95
        assert vm["ai_status"] == "success"

    def test_view_model_no_result(self, ai_service):
        vm = ai_service.get_ai_view_model("nonexistent_run")
        assert vm["has_result"] is False

    def test_malformed_response_yields_invalid_output(
        self, ai_service, fake_audio_file, tmp_path
    ):
        """Toolchain writes bad JSON response => invalid_output."""
        bad_script = tmp_path / "bad_toolchain.py"
        bad_script.write_text(
            '''
import sys
# Parse --response arg
resp_path = None
for i, a in enumerate(sys.argv):
    if a == "--response":
        resp_path = sys.argv[i+1]
with open(resp_path, "w") as f:
    f.write("NOT VALID JSON")
''',
            encoding="utf-8",
        )

        cfg = TestConfigTable.create(config_name="Bad Resp Test")
        tc = AiToolchainTable.create(
            toolchain_id="bad-resp-tc",
            display_name="Bad Resp TC", version="1",
            entrypoint=f"{sys.executable} {bad_script}",
            default_timeout_ms=10000,
        )
        TestConfigAiBindingTable.create(
            config=cfg, toolchain=tc, enabled=True, params_json="{}"
        )

        segments = [{
            "segment_id": "d1", "role": "device_1",
            "audio_path": fake_audio_file, "channels": [1],
            "duration_ms": 5000, "sample_rate": 48000, "metadata": {},
        }]

        result = ai_service.run_ai_analysis(
            config_id=cfg.config_id, test_run_id="run_bad_resp",
            segments=segments, context={},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )
        assert result["module_status"] == "invalid_output"
        assert result["request_json_path"] is not None
        assert result["artifacts_dir"] is not None

    def test_not_enabled_short_circuit_no_process(self, ai_service, fake_audio_file, tmp_path):
        """not_enabled should not launch any process."""
        cfg = TestConfigTable.create(config_name="Disabled Test")
        tc = AiToolchainTable.create(
            toolchain_id="dis-tc-int",
            display_name="Dis TC Int", version="1",
            entrypoint="this_should_not_run",
            default_timeout_ms=10000,
        )
        TestConfigAiBindingTable.create(
            config=cfg, toolchain=tc, enabled=False, params_json="{}"
        )

        result = ai_service.run_ai_analysis(
            config_id=cfg.config_id, test_run_id="run_disabled",
            segments=[{"audio_path": fake_audio_file}],
            context={},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )
        assert result["module_status"] == "not_enabled"
        # No process launched, so no file paths
        assert result.get("request_json_path") is None
        assert result.get("artifacts_dir") is None

    def test_success_then_failure_clears_effective(
        self, ai_service, fake_audio_file, fake_toolchain_script, tmp_path
    ):
        """After a successful run, a failed rerun leaves no current_effective record."""
        cfg = TestConfigTable.create(config_name="Fail After Success")
        tc = AiToolchainTable.create(
            toolchain_id="fas-tc",
            display_name="FAS TC", version="1",
            entrypoint=f"{sys.executable} {fake_toolchain_script}",
            default_timeout_ms=10000,
        )
        TestConfigAiBindingTable.create(
            config=cfg, toolchain=tc, enabled=True, params_json="{}"
        )
        segments = [{
            "segment_id": "d1", "role": "device_1",
            "audio_path": fake_audio_file, "channels": [1],
            "duration_ms": 5000, "sample_rate": 48000, "metadata": {},
        }]

        # First run succeeds
        r1 = ai_service.run_ai_analysis(
            config_id=cfg.config_id, test_run_id="run_fas",
            segments=segments, context={},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )
        assert r1["module_status"] == "success"
        assert r1["is_current_effective"] is True

        # Switch to a bad toolchain for the second run
        bad_script = tmp_path / "bad_tc.py"
        bad_script.write_text(
            'import sys\n'
            'resp = None\n'
            'for i, a in enumerate(sys.argv):\n'
            '    if a == "--response": resp = sys.argv[i+1]\n'
            'with open(resp, "w") as f: f.write("NOT JSON")\n',
            encoding="utf-8",
        )
        tc.entrypoint = f"{sys.executable} {bad_script}"
        tc.save()

        # Second run fails
        r2 = ai_service.run_ai_analysis(
            config_id=cfg.config_id, test_run_id="run_fas",
            segments=segments, context={},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )
        assert r2["module_status"] == "invalid_output"
        assert r2["is_current_effective"] is False

        # Old success record should also be cleared
        old = AiExecutionTable.get_by_id(r1["request_id"])
        assert old.is_current_effective is False

        # No effective record at all
        effective_count = (
            AiExecutionTable.select()
            .where(
                AiExecutionTable.test_run_id == "run_fas",
                AiExecutionTable.is_current_effective == True,
            )
            .count()
        )
        assert effective_count == 0

        # ViewModel falls back to latest failed execution
        vm = ai_service.get_ai_view_model("run_fas")
        assert vm["has_result"] is True
        assert vm["display_status"] == "invalid_output"
        assert vm["error_summary"] is not None
