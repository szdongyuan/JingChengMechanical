"""Integration test for AiService.run_ai_analysis end-to-end."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid

import pytest

from model.test_config_table import TestConfigTable
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable
from model.ai_execution_table import AiExecutionTable
from app.service.ai_service import AiService


@pytest.fixture
def ai_service():
    service = AiService()
    try:
        yield service
    finally:
        if hasattr(service, "_resident_runtime"):
            service._resident_runtime.shutdown_all()


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


_RESIDENT_TOOLCHAIN_SCRIPT = """
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path


INSTANCE_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


def _arg(name: str) -> str | None:
    if name not in sys.argv:
        return None
    index = sys.argv.index(name)
    if index + 1 >= len(sys.argv):
        return None
    return sys.argv[index + 1]


def _write_artifact(artifacts_dir: str, relative_path: str) -> None:
    target = Path(artifacts_dir) / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("artifact", encoding="utf-8")


def _success_payload(request_id: str, *, label: str, transport: str, artifact_name: str) -> dict:
    return {
        "protocol_version": 1,
        "request_id": request_id,
        "toolchain_name": f"{transport}-toolchain",
        "toolchain_version": "1.0",
        "ai_result": {
            "ai_label": label,
            "score": 0.95,
            "summary": f"{transport} success",
            "confidence": 0.9,
            "artifacts": [
                {
                    "type": "debug",
                    "relative_path": artifact_name,
                }
            ],
            "extra": {
                "transport": transport,
                "process_instance_id": INSTANCE_ID,
            },
        },
    }


request_path = _arg("--request")
response_path = _arg("--response")
artifacts_dir = _arg("--artifacts-dir")
if request_path and response_path and artifacts_dir:
    with open(request_path, "r", encoding="utf-8") as f:
        request_payload = json.load(f)
    _write_artifact(artifacts_dir, "fallback.txt")
    with open(response_path, "w", encoding="utf-8") as f:
        json.dump(
            _success_payload(
                request_payload["request_id"],
                label="FALLBACK",
                transport="oneshot_fallback",
                artifact_name="fallback.txt",
            ),
            f,
            ensure_ascii=False,
            indent=2,
        )
    raise SystemExit(0)


for raw_line in sys.stdin:
    line = raw_line.rstrip("\\r\\n")
    if not line:
        continue
    message = json.loads(line)
    message_type = message.get("message_type")
    if message_type == "shutdown":
        break
    if message_type != "analyze":
        continue

    request_id = message.get("request_id")
    request_payload = message.get("request") or {}
    params = request_payload.get("params") or {}
    behavior = params.get("behavior", "success")
    artifacts_dir = message.get("artifacts_dir")

    if behavior == "error":
        _emit(
            {
                "message_type": "error",
                "request_id": request_id,
                "error": "resident child reported failure",
            }
        )
        continue

    if behavior == "malformed_json":
        sys.stdout.write("{not valid json\\n")
        sys.stdout.flush()
        continue

    if behavior == "timeout":
        time.sleep(float(params.get("sleep_s", 0.3)))

    if behavior == "block":
        Path(params["started_file"]).write_text("started", encoding="utf-8")
        release_file = Path(params["release_file"])
        while not release_file.exists():
            time.sleep(0.01)

    _write_artifact(artifacts_dir, "resident.txt")
    _emit(
        {
            "message_type": "result",
            "request_id": request_id,
            "response": _success_payload(
                request_id,
                label="RESIDENT_PASS",
                transport="resident",
                artifact_name="resident.txt",
            ),
        }
    )
"""


def _write_resident_toolchain_script(tmp_path):
    script = tmp_path / f"resident_toolchain_{uuid.uuid4().hex[:8]}.py"
    script.write_text(textwrap.dedent(_RESIDENT_TOOLCHAIN_SCRIPT), encoding="utf-8")
    return script


def _resident_entrypoint(script_path) -> str:
    return subprocess.list2cmdline([sys.executable, "-u", str(script_path)])


def _analysis_segments(audio_path: str) -> list[dict]:
    return [{
        "segment_id": "dev_1",
        "role": "device_1",
        "audio_path": audio_path,
        "channels": [1],
        "duration_ms": 5000,
        "sample_rate": 48000,
        "metadata": {},
    }]


def _wait_for_file(path, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {path}")


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
        execution_mode="oneshot_process",
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
            execution_mode="oneshot_process",
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
            execution_mode="oneshot_process",
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
            execution_mode="oneshot_process",
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
            execution_mode="oneshot_process",
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
            execution_mode="oneshot_process",
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
            execution_mode="oneshot_process",
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
            execution_mode="oneshot_process",
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

    def test_resident_process_reuses_cached_runtime_and_persists_request_response_and_artifacts(
        self, ai_service, fake_audio_file, tmp_path
    ):
        cfg = TestConfigTable.create(config_name="Resident Success")
        script_path = _write_resident_toolchain_script(tmp_path)
        tc = AiToolchainTable.create(
            toolchain_id="resident-success-tc",
            display_name="Resident Success TC",
            version="1.0",
            entrypoint=_resident_entrypoint(script_path),
            default_timeout_ms=1000,
            execution_mode="resident_process",
        )
        TestConfigAiBindingTable.create(
            config=cfg,
            toolchain=tc,
            enabled=True,
            params_json=json.dumps({"behavior": "success"}),
        )
        segments = _analysis_segments(fake_audio_file)

        first = ai_service.run_ai_analysis(
            config_id=cfg.config_id,
            test_run_id="run_resident_success_1",
            segments=segments,
            context={"config_id": cfg.config_id},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )
        second = ai_service.run_ai_analysis(
            config_id=cfg.config_id,
            test_run_id="run_resident_success_2",
            segments=segments,
            context={"config_id": cfg.config_id},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )

        first_extra = json.loads(first["extra_json"])
        second_extra = json.loads(second["extra_json"])

        assert first["module_status"] == "success"
        assert second["module_status"] == "success"
        assert first_extra["transport"] == "resident"
        assert second_extra["transport"] == "resident"
        assert first_extra["process_instance_id"] == second_extra["process_instance_id"]
        assert ai_service._resident_runtime.has_cached_process("resident-success-tc") is True

        for result in (first, second):
            assert os.path.isfile(result["request_json_path"])
            assert os.path.isfile(result["response_json_path"])
            assert os.path.isdir(result["artifacts_dir"])
            assert os.path.isfile(os.path.join(result["artifacts_dir"], "resident.txt"))
            with open(result["response_json_path"], "r", encoding="utf-8") as f:
                response_payload = json.load(f)
            assert response_payload["ai_result"]["ai_label"] == "RESIDENT_PASS"
            assert response_payload["ai_result"]["extra"]["transport"] == "resident"

    def test_resident_process_message_type_error_returns_process_error_without_response_json_or_oneshot_fallback(
        self, ai_service, fake_audio_file, tmp_path
    ):
        cfg = TestConfigTable.create(config_name="Resident Error")
        script_path = _write_resident_toolchain_script(tmp_path)
        tc = AiToolchainTable.create(
            toolchain_id="resident-error-tc",
            display_name="Resident Error TC",
            version="1.0",
            entrypoint=_resident_entrypoint(script_path),
            default_timeout_ms=1000,
            execution_mode="resident_process",
        )
        TestConfigAiBindingTable.create(
            config=cfg,
            toolchain=tc,
            enabled=True,
            params_json=json.dumps({"behavior": "error"}),
        )

        result = ai_service.run_ai_analysis(
            config_id=cfg.config_id,
            test_run_id="run_resident_error",
            segments=_analysis_segments(fake_audio_file),
            context={"config_id": cfg.config_id},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )

        assert result["module_status"] == "process_error"
        assert result["error_summary"] == "resident child reported failure"
        assert os.path.isfile(result["request_json_path"])
        assert os.path.exists(result["response_json_path"]) is False
        assert result["toolchain_name"] is None
        assert result["ai_label"] is None
        assert ai_service._resident_runtime.has_cached_process("resident-error-tc") is False

    def test_resident_process_malformed_json_returns_protocol_error(
        self, ai_service, fake_audio_file, tmp_path
    ):
        cfg = TestConfigTable.create(config_name="Resident Malformed")
        script_path = _write_resident_toolchain_script(tmp_path)
        tc = AiToolchainTable.create(
            toolchain_id="resident-malformed-tc",
            display_name="Resident Malformed TC",
            version="1.0",
            entrypoint=_resident_entrypoint(script_path),
            default_timeout_ms=1000,
            execution_mode="resident_process",
        )
        TestConfigAiBindingTable.create(
            config=cfg,
            toolchain=tc,
            enabled=True,
            params_json=json.dumps({"behavior": "malformed_json"}),
        )

        result = ai_service.run_ai_analysis(
            config_id=cfg.config_id,
            test_run_id="run_resident_malformed",
            segments=_analysis_segments(fake_audio_file),
            context={"config_id": cfg.config_id},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )

        assert result["module_status"] == "protocol_error"
        assert "JSON" in result["error_summary"]
        assert os.path.exists(result["response_json_path"]) is False
        assert ai_service._resident_runtime.has_cached_process("resident-malformed-tc") is False

    def test_resident_process_timeout_returns_timeout_and_discards_cache(
        self, ai_service, fake_audio_file, tmp_path
    ):
        cfg = TestConfigTable.create(config_name="Resident Timeout")
        script_path = _write_resident_toolchain_script(tmp_path)
        tc = AiToolchainTable.create(
            toolchain_id="resident-timeout-tc",
            display_name="Resident Timeout TC",
            version="1.0",
            entrypoint=_resident_entrypoint(script_path),
            default_timeout_ms=1000,
            execution_mode="resident_process",
        )
        TestConfigAiBindingTable.create(
            config=cfg,
            toolchain=tc,
            enabled=True,
            timeout_ms=50,
            params_json=json.dumps({"behavior": "timeout", "sleep_s": 0.3}),
        )

        result = ai_service.run_ai_analysis(
            config_id=cfg.config_id,
            test_run_id="run_resident_timeout",
            segments=_analysis_segments(fake_audio_file),
            context={"config_id": cfg.config_id},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )

        assert result["module_status"] == "timeout"
        assert result["error_summary"] == "Process timed out"
        assert os.path.exists(result["response_json_path"]) is False
        assert ai_service._resident_runtime.has_cached_process("resident-timeout-tc") is False

    def test_resident_process_busy_overlap_returns_process_error_without_fallback(
        self, ai_service, fake_audio_file, tmp_path, monkeypatch
    ):
        cfg = TestConfigTable.create(config_name="Resident Busy")
        script_path = _write_resident_toolchain_script(tmp_path)
        tc = AiToolchainTable.create(
            toolchain_id="resident-busy-tc",
            display_name="Resident Busy TC",
            version="1.0",
            entrypoint=_resident_entrypoint(script_path),
            default_timeout_ms=5000,
            execution_mode="resident_process",
        )
        started_file = tmp_path / "resident-started.txt"
        release_file = tmp_path / "resident-release.txt"
        TestConfigAiBindingTable.create(
            config=cfg,
            toolchain=tc,
            enabled=True,
            params_json=json.dumps(
                {
                    "behavior": "block",
                    "started_file": str(started_file),
                    "release_file": str(release_file),
                }
            ),
        )
        binding = {
            "binding_id": 1,
            "config_id": cfg.config_id,
            "enabled": True,
            "toolchain_id": tc.toolchain_id,
            "timeout_ms": None,
            "effective_timeout_ms": 5000,
            "params_json": json.dumps(
                {
                    "behavior": "block",
                    "started_file": str(started_file),
                    "release_file": str(release_file),
                }
            ),
        }
        monkeypatch.setattr(
            ai_service,
            "preflight_check",
            lambda config_id, segments: {
                "status": "ok",
                "binding": binding,
                "toolchain": tc,
            },
        )
        monkeypatch.setattr(ai_service, "mark_previous_ineffective", lambda test_run_id: None)
        monkeypatch.setattr(ai_service, "save_execution", lambda envelope: envelope)
        first_result = {}
        segments = _analysis_segments(fake_audio_file)

        def _run_first():
            first_result.update(
                ai_service.run_ai_analysis(
                    config_id=cfg.config_id,
                    test_run_id="run_resident_busy_first",
                    segments=segments,
                    context={"config_id": cfg.config_id},
                    ai_runs_dir=str(tmp_path / "ai_runs"),
                )
            )

        worker = threading.Thread(target=_run_first)
        worker.start()
        _wait_for_file(started_file)

        started = time.monotonic()
        second = ai_service.run_ai_analysis(
            config_id=cfg.config_id,
            test_run_id="run_resident_busy_second",
            segments=segments,
            context={"config_id": cfg.config_id},
            ai_runs_dir=str(tmp_path / "ai_runs"),
        )
        elapsed = time.monotonic() - started

        release_file.write_text("release", encoding="utf-8")
        worker.join(timeout=5)

        assert second["module_status"] == "process_error"
        assert second["error_summary"] == "Resident toolchain is busy"
        assert elapsed < 0.5
        assert os.path.exists(second["response_json_path"]) is False
        assert first_result["module_status"] == "success"
