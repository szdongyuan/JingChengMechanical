from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.service.ai_resident_runtime import ResidentToolchainRuntime


_FAKE_RESIDENT_SCRIPT = """
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path


MODE = sys.argv[1] if len(sys.argv) > 1 else "driver"
SHUTDOWN_MARKER = sys.argv[2] if len(sys.argv) > 2 else ""
INSTANCE_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def emit_text(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def emit_message(payload: dict) -> None:
    emit_text(json.dumps(payload) + "\\n")


def write_stderr(text: str) -> None:
    if text:
        sys.stderr.write(text)
        sys.stderr.flush()


def build_response(request: dict) -> dict:
    params = request.get("params") or {}
    ai_result = {
        "ai_label": "ok",
        "process_instance_id": INSTANCE_ID,
    }
    if params.get("include_launch_details"):
        ai_result["argv"] = sys.argv[1:]
        ai_result["cwd"] = os.getcwd()
        ai_result["env_value"] = os.environ.get("RESIDENT_RUNTIME_TEST_ENV")
    return {
        "protocol_version": 1,
        "request_id": request["request_id"],
        "toolchain_name": "fake-resident",
        "toolchain_version": "1.0",
        "ai_result": ai_result,
    }


def handle_analyze(message: dict) -> None:
    request_id = message.get("request_id")
    request = message.get("request") or {}
    params = request.get("params") or {}
    behavior = params.get("behavior", "success")
    stderr_text = params.get("stderr_text", "")
    stderr_repeat = int(params.get("stderr_repeat", 1))
    for _ in range(stderr_repeat):
        write_stderr(stderr_text)

    if behavior == "block":
        Path(params["started_file"]).write_text("started", encoding="utf-8")
        release_file = Path(params["release_file"])
        while not release_file.exists():
            time.sleep(0.01)
        emit_message(
            {
                "message_type": "result",
                "request_id": request_id,
                "response": build_response(request),
            }
        )
        return

    if behavior == "error":
        emit_message(
            {
                "message_type": "error",
                "request_id": request_id,
                "error": "child reported failure",
            }
        )
        return

    if behavior == "malformed_json":
        emit_text("{not valid json\\n")
        return

    if behavior == "unexpected_type":
        emit_message(
            {
                "message_type": "unexpected",
                "request_id": request_id,
            }
        )
        return

    if behavior == "missing_request_id":
        emit_message(
            {
                "message_type": "result",
                "response": build_response(request),
            }
        )
        return

    if behavior == "mismatched_request_id":
        emit_message(
            {
                "message_type": "result",
                "request_id": "wrong-request",
                "response": build_response(request),
            }
        )
        return

    if behavior == "timeout":
        time.sleep(float(params.get("sleep_s", 0.3)))
        emit_message(
            {
                "message_type": "result",
                "request_id": request_id,
                "response": build_response(request),
            }
        )
        return

    if behavior == "extra_stdout_after_success":
        emit_message(
            {
                "message_type": "result",
                "request_id": request_id,
                "response": build_response(request),
            }
        )
        time.sleep(float(params.get("extra_stdout_delay_s", 0.05)))
        emit_text("idle stdout noise\\n")
        return

    emit_message(
        {
            "message_type": "result",
            "request_id": request_id,
            "response": build_response(request),
        }
    )


for raw_line in sys.stdin:
    line = raw_line.rstrip("\\r\\n")
    if not line:
        continue
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        emit_text("protocol parse failure\\n")
        continue

    message_type = message.get("message_type")
    if message_type == "shutdown":
        if SHUTDOWN_MARKER:
            Path(SHUTDOWN_MARKER).write_text("shutdown", encoding="utf-8")
        if MODE == "ignore-shutdown":
            while True:
                time.sleep(0.1)
        break
    if message_type == "analyze":
        handle_analyze(message)
"""


def _write_fake_resident_script(tmp_path: Path) -> Path:
    script_path = tmp_path / "fake_resident_toolchain.py"
    script_path.write_text(textwrap.dedent(_FAKE_RESIDENT_SCRIPT), encoding="utf-8")
    return script_path


def _entrypoint(script_path: Path, *extra_args: str) -> str:
    return subprocess.list2cmdline(
        [sys.executable, "-u", str(script_path), *extra_args]
    )


def _request_payload(
    request_id: str,
    *,
    toolchain_id: str = "tc-1",
    behavior: str = "success",
    **params,
) -> dict:
    request_params = {"behavior": behavior}
    request_params.update(params)
    return {
        "protocol_version": 1,
        "request_id": request_id,
        "test_run_id": "run-1",
        "toolchain_id": toolchain_id,
        "segments": [],
        "context": {},
        "params": request_params,
        "window_info": {},
    }


def _wait_for_file(path: Path, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {path}")


@pytest.fixture
def runtime():
    helper = ResidentToolchainRuntime()
    try:
        yield helper
    finally:
        helper.shutdown_all()


def test_reuses_cached_process_for_repeated_requests(tmp_path: Path, runtime):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)

    first = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1"),
        artifacts_dir=str(tmp_path / "artifacts-1"),
        timeout_ms=1000,
    )
    second = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-2",
        request_payload=_request_payload("req-2"),
        artifacts_dir=str(tmp_path / "artifacts-2"),
        timeout_ms=1000,
    )

    assert first["module_status"] == "success"
    assert second["module_status"] == "success"
    assert second["process_instance_id"] == first["process_instance_id"]
    assert first["stdout_excerpt"] is None
    assert second["stdout_excerpt"] is None
    assert first["response_payload"]["ai_result"]["process_instance_id"]
    assert runtime.has_cached_process("tc-1") is True


def test_busy_second_request_fails_fast(tmp_path: Path, runtime):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    started_file = tmp_path / "started.txt"
    release_file = tmp_path / "release.txt"
    first_result: dict[str, object] = {}

    def run_first() -> None:
        first_result.update(
            runtime.analyze(
                toolchain_id="tc-1",
                entrypoint=entrypoint,
                request_id="req-1",
                request_payload=_request_payload(
                    "req-1",
                    behavior="block",
                    started_file=str(started_file),
                    release_file=str(release_file),
                ),
                artifacts_dir=str(tmp_path / "artifacts-1"),
                timeout_ms=5000,
            )
        )

    worker = threading.Thread(target=run_first)
    worker.start()
    _wait_for_file(started_file)

    started = time.monotonic()
    busy = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-2",
        request_payload=_request_payload("req-2"),
        artifacts_dir=str(tmp_path / "artifacts-2"),
        timeout_ms=5000,
    )
    elapsed = time.monotonic() - started

    with pytest.raises(ValueError, match="busy"):
        runtime.ensure_mutation_allowed("tc-1")

    release_file.write_text("release", encoding="utf-8")
    worker.join(timeout=5)

    assert busy["module_status"] == "process_error"
    assert busy["error_summary"] == "Resident toolchain is busy"
    assert elapsed < 0.5
    assert first_result["module_status"] == "success"


def test_message_type_error_maps_to_process_error_and_discards_cache(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1", behavior="error"),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=1000,
    )

    assert result["module_status"] == "process_error"
    assert result["error_summary"] == "child reported failure"
    assert runtime.has_cached_process("tc-1") is False


def test_launches_entrypoint_exactly_as_stored_without_extra_flags(
    tmp_path: Path, runtime, monkeypatch
):
    script_path = _write_fake_resident_script(tmp_path)
    expected_cwd = tmp_path / "child-cwd"
    expected_cwd.mkdir()
    monkeypatch.chdir(expected_cwd)
    monkeypatch.setenv("RESIDENT_RUNTIME_TEST_ENV", "inherited-value")
    entrypoint = _entrypoint(script_path, "launch-introspect", "stored-arg")

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            include_launch_details=True,
        ),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=1000,
    )

    ai_result = result["response_payload"]["ai_result"]

    assert result["module_status"] == "success"
    assert ai_result["argv"] == ["launch-introspect", "stored-arg"]
    assert ai_result["cwd"] == str(expected_cwd)
    assert ai_result["env_value"] == "inherited-value"


def test_malformed_json_line_returns_protocol_error_and_discards_cache(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1", behavior="malformed_json"),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=1000,
    )

    assert result["module_status"] == "protocol_error"
    assert "JSON" in result["error_summary"]
    assert runtime.has_cached_process("tc-1") is False


def test_unexpected_message_type_returns_protocol_error_and_discards_cache(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1", behavior="unexpected_type"),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=1000,
    )

    assert result["module_status"] == "protocol_error"
    assert "message_type" in result["error_summary"]
    assert runtime.has_cached_process("tc-1") is False


@pytest.mark.parametrize("behavior", ["missing_request_id", "mismatched_request_id"])
def test_missing_or_mismatched_request_id_returns_protocol_error(
    tmp_path: Path, runtime, behavior: str
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1", behavior=behavior),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=1000,
    )

    assert result["module_status"] == "protocol_error"
    assert "request_id" in result["error_summary"]
    assert runtime.has_cached_process("tc-1") is False


def test_timeout_waiting_for_response_discards_cache(tmp_path: Path, runtime):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            behavior="timeout",
            sleep_s=0.3,
        ),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=50,
    )

    assert result["module_status"] == "timeout"
    assert result["error_summary"] == "Process timed out"
    assert runtime.has_cached_process("tc-1") is False


def test_idle_extra_stdout_invalidates_cache_before_next_reuse(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)

    first = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1", behavior="extra_stdout_after_success"),
        artifacts_dir=str(tmp_path / "artifacts-1"),
        timeout_ms=1000,
    )
    time.sleep(0.2)
    second = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-2",
        request_payload=_request_payload("req-2"),
        artifacts_dir=str(tmp_path / "artifacts-2"),
        timeout_ms=1000,
    )

    assert first["module_status"] == "success"
    assert second["module_status"] == "success"
    assert second["process_instance_id"] != first["process_instance_id"]


def test_stderr_capture_is_per_run_truncated_and_buffered(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)

    first = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            stderr_text="A" * 70000,
        ),
        artifacts_dir=str(tmp_path / "artifacts-1"),
        timeout_ms=2000,
    )
    second = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-2",
        request_payload=_request_payload(
            "req-2",
            stderr_text="second-run-stderr",
        ),
        artifacts_dir=str(tmp_path / "artifacts-2"),
        timeout_ms=1000,
    )

    assert first["module_status"] == "success"
    assert len(first["stderr_excerpt"]) == 2000
    assert first["stderr_excerpt"] == "A" * 2000
    assert second["stderr_excerpt"] == "second-run-stderr"
    assert runtime._processes["tc-1"].stderr_buffer_char_count <= 64 * 1024


def test_shutdown_all_sends_best_effort_shutdown_and_forces_terminate_after_grace_period(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    shutdown_marker = tmp_path / "shutdown-marker.txt"
    entrypoint = _entrypoint(script_path, "ignore-shutdown", str(shutdown_marker))

    first = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1"),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=1000,
    )
    cached_process = runtime._processes["tc-1"].process

    started = time.monotonic()
    runtime.shutdown_all()
    elapsed = time.monotonic() - started

    assert first["module_status"] == "success"
    assert shutdown_marker.read_text(encoding="utf-8") == "shutdown"
    assert cached_process.poll() is not None
    assert elapsed >= 1.8
    assert runtime.has_cached_process("tc-1") is False
