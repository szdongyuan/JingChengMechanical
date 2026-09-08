from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.service.ai_resident_runtime as ai_resident_runtime
from app.service.ai_resident_runtime import ResidentToolchainRuntime
from app.service.analysis.scheduler import (
    AiToolchainSnapshot,
    canonical_toolchain_lane_key,
)


_FAKE_RESIDENT_SCRIPT = """
from __future__ import annotations

import json
import os
import sys
import time
import threading
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


def write_slow_stderr(text: str, repeat: int, delay_s: float, marker_file: str = "") -> None:
    for _ in range(repeat):
        write_stderr(text)
        time.sleep(delay_s)
    if marker_file:
        Path(marker_file).write_text("written", encoding="utf-8")


startup_stderr_text = os.environ.get("RESIDENT_RUNTIME_TEST_STARTUP_STDERR", "")
startup_stderr_once_file = os.environ.get(
    "RESIDENT_RUNTIME_TEST_STARTUP_STDERR_ONCE_FILE", ""
)
if startup_stderr_text:
    if not startup_stderr_once_file or not Path(startup_stderr_once_file).exists():
        write_stderr(startup_stderr_text)
        if startup_stderr_once_file:
            Path(startup_stderr_once_file).write_text("written", encoding="utf-8")


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

    if behavior == "stderr_after_success":
        emit_message(
            {
                "message_type": "result",
                "request_id": request_id,
                "response": build_response(request),
            }
        )
        time.sleep(float(params.get("late_stderr_delay_s", 0.05)))
        write_stderr(params.get("late_stderr_text", "late stderr"))
        late_stderr_written_file = params.get("late_stderr_written_file")
        if late_stderr_written_file:
            Path(late_stderr_written_file).write_text("written", encoding="utf-8")
        return

    if behavior == "slow_stderr_after_success":
        thread = threading.Thread(
            target=write_slow_stderr,
            args=(
                params.get("late_stderr_text", "late stderr"),
                int(params.get("late_stderr_repeat", 40)),
                float(params.get("late_stderr_delay_s", 0.01)),
                params.get("late_stderr_written_file", ""),
            ),
            daemon=True,
        )
        thread.start()
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

    if behavior == "extra_stdout_after_gate":
        emit_message(
            {
                "message_type": "result",
                "request_id": request_id,
                "response": build_response(request),
            }
        )
        release_file = Path(params["extra_stdout_release_file"])
        while not release_file.exists():
            time.sleep(0.01)
        emit_text(params.get("extra_stdout_text", "unexpected extra stdout") + "\\n")
        return

    if behavior == "extra_stdout_during_active_request":
        emit_text(
            json.dumps(
                {
                    "message_type": "result",
                    "request_id": request_id,
                    "response": build_response(request),
                }
            )
            + "\\n"
            + "unexpected extra stdout\\n"
        )
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
    if message_type == "ready":
        emit_message(
            {
                "message_type": "ready",
                "protocol_version": 1,
                "execution_revision": message.get("execution_revision"),
                "process_instance_id": INSTANCE_ID,
            }
        )
        continue
    if message_type == "analyze":
        handle_analyze(message)
"""


def _write_fake_resident_script(tmp_path: Path) -> Path:
    script_path = tmp_path / "fake_resident_toolchain.py"
    script_path.write_text(textwrap.dedent(_FAKE_RESIDENT_SCRIPT), encoding="utf-8")
    return script_path


_LEGACY_RESIDENT_SCRIPT = """
from __future__ import annotations

import json
import os
import json
import sys
import time


if len(sys.argv) > 1 and sys.argv[1] == "hang-before-loop":
    time.sleep(30.0)

for raw_line in sys.stdin:
    message = json.loads(raw_line)
    message_type = message.get("message_type")
    if message_type == "shutdown":
        break
    if message_type != "analyze":
        continue
    request = message["request"]
    sys.stdout.write(
        json.dumps(
            {
                "message_type": "result",
                "request_id": message["request_id"],
                "response": {
                    "protocol_version": 1,
                    "request_id": request["request_id"],
                    "toolchain_name": "legacy-resident",
                    "toolchain_version": "0",
                    "ai_result": {
                        "ai_label": "ok",
                        "process_id": os.getpid(),
                    },
                },
            }
        )
        + "\\n"
    )
    sys.stdout.flush()
"""


def _write_legacy_resident_script(tmp_path: Path) -> Path:
    script_path = tmp_path / "legacy_resident_toolchain.py"
    script_path.write_text(
        textwrap.dedent(_LEGACY_RESIDENT_SCRIPT), encoding="utf-8"
    )
    return script_path


def _write_legacy_jsonl_manifest(
    tmp_path: Path,
    entrypoint_file: str,
) -> None:
    (tmp_path / "toolchain_manifest.json").write_text(
        json.dumps(
            {
                "entrypoint_file": entrypoint_file,
                "execution_mode": "resident_process",
                "protocol": "UTF-8 JSONL over stdin/stdout",
            }
        ),
        encoding="utf-8",
    )


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


def test_start_process_preserves_pipes_and_uses_no_console_flags(
    runtime, monkeypatch
):
    captured = {}

    def _popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(stdin=object(), stdout=object(), stderr=object())

    class _Thread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(ai_resident_runtime.subprocess, "Popen", _popen)
    monkeypatch.setattr(ai_resident_runtime.threading, "Thread", _Thread)
    monkeypatch.setattr(
        ai_resident_runtime,
        "ai_subprocess_creation_flags",
        lambda: 0x08000000,
    )

    runtime._start_process("tc-1", "tool.exe --serve", package_dir="C:/AI")

    assert captured["kwargs"] == {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
        "cwd": "C:/AI",
        "creationflags": 0x08000000,
    }


def _toolchain_snapshot(
    tmp_path: Path,
    entrypoint: str,
    *,
    revision: str = "revision-1",
) -> AiToolchainSnapshot:
    return AiToolchainSnapshot(
        toolchain_id="tc-1",
        runtime_lane_key=canonical_toolchain_lane_key("tc-1"),
        execution_revision=revision,
        execution_mode="resident_process",
        entrypoint=entrypoint,
        package_dir=str(tmp_path),
        params_json="{}",
        timeout_seconds=2.0,
    )


def test_prewarm_is_idempotent_and_revision_exact_without_business_request(
    tmp_path: Path,
    runtime,
    monkeypatch,
):
    script_path = _write_fake_resident_script(tmp_path)
    snapshot = _toolchain_snapshot(tmp_path, _entrypoint(script_path))
    monkeypatch.setattr(
        runtime,
        "_send_request",
        lambda **_kwargs: pytest.fail("prewarm sent a business analysis request"),
    )

    first_instance = runtime.prewarm(snapshot)
    second_instance = runtime.prewarm(snapshot)

    assert first_instance == second_instance
    assert runtime.readiness(snapshot) is True
    assert runtime.process_instance_id(snapshot) == first_instance
    assert len(runtime._processes) == 1

    changed = _toolchain_snapshot(
        tmp_path,
        _entrypoint(script_path),
        revision="revision-2",
    )
    changed_instance = runtime.prewarm(changed)

    assert changed_instance != first_instance
    assert runtime.readiness(snapshot) is True
    assert runtime.readiness(changed) is True
    assert len(runtime._processes) == 2


def test_manifested_legacy_jsonl_resident_uses_drain_proof_and_stays_warm(
    tmp_path: Path,
    runtime,
):
    script_path = _write_legacy_resident_script(tmp_path)
    _write_legacy_jsonl_manifest(tmp_path, Path(sys.executable).name)
    snapshot = _toolchain_snapshot(tmp_path, _entrypoint(script_path))

    instance_id = runtime.prewarm(snapshot)
    first = runtime.analyze(
        toolchain_id=snapshot.toolchain_id,
        entrypoint=snapshot.entrypoint,
        package_dir=snapshot.package_dir,
        execution_revision=snapshot.execution_revision,
        required_instance_id=instance_id,
        request_id="legacy-1",
        request_payload=_request_payload("legacy-1"),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=2_000,
    )
    second = runtime.analyze(
        toolchain_id=snapshot.toolchain_id,
        entrypoint=snapshot.entrypoint,
        package_dir=snapshot.package_dir,
        execution_revision=snapshot.execution_revision,
        required_instance_id=instance_id,
        request_id="legacy-2",
        request_payload=_request_payload("legacy-2"),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=2_000,
    )

    assert runtime.readiness(snapshot) is True
    assert first["module_status"] == "success"
    assert second["module_status"] == "success"
    assert (
        first["response_payload"]["ai_result"]["process_id"]
        == second["response_payload"]["ai_result"]["process_id"]
    )
    assert runtime.process_instance_id(snapshot) == instance_id


def test_legacy_drain_fallback_requires_explicit_jsonl_resident_manifest(
    tmp_path: Path,
    runtime,
):
    script_path = _write_legacy_resident_script(tmp_path)
    snapshot = _toolchain_snapshot(tmp_path, _entrypoint(script_path))

    with pytest.raises(TimeoutError, match="resident stdout"):
        runtime.prewarm(snapshot)

    assert runtime.readiness(snapshot) is False


def test_manifested_legacy_process_that_never_drains_probe_is_not_ready(
    tmp_path: Path,
):
    runtime = ResidentToolchainRuntime()
    try:
        script_path = _write_legacy_resident_script(tmp_path)
        _write_legacy_jsonl_manifest(tmp_path, Path(sys.executable).name)
        snapshot = AiToolchainSnapshot(
            toolchain_id="tc-1",
            runtime_lane_key=canonical_toolchain_lane_key("tc-1"),
            execution_revision="revision-1",
            execution_mode="resident_process",
            entrypoint=_entrypoint(script_path, "hang-before-loop"),
            package_dir=str(tmp_path),
            params_json="{}",
            timeout_seconds=0.4,
        )

        with pytest.raises(TimeoutError):
            runtime.prewarm(snapshot)

        assert runtime.readiness(snapshot) is False
    finally:
        runtime.shutdown_all()


def test_unexpected_ready_process_exit_revokes_and_coalesces_one_restart(
    tmp_path: Path,
):
    readiness_events = []
    runtime = ResidentToolchainRuntime(
        readiness_changed=lambda snapshot, ready: readiness_events.append(
            (snapshot.execution_revision, ready)
        )
    )
    try:
        script_path = _write_fake_resident_script(tmp_path)
        snapshot = _toolchain_snapshot(tmp_path, _entrypoint(script_path))
        first_instance = runtime.prewarm(snapshot)
        cached = next(iter(runtime._processes.values()))

        cached.process.kill()
        cached.process.wait(timeout=2.0)
        deadline = time.monotonic() + 4.0
        saw_revoked = False
        while time.monotonic() < deadline:
            saw_revoked = saw_revoked or (snapshot.execution_revision, False) in readiness_events
            if saw_revoked and runtime.readiness(snapshot):
                break
            time.sleep(0.01)

        assert saw_revoked
        assert runtime.readiness(snapshot) is True
        assert runtime.process_instance_id(snapshot) != first_instance
        assert readiness_events.count((snapshot.execution_revision, False)) == 1
        assert readiness_events.count((snapshot.execution_revision, True)) == 2
    finally:
        runtime.shutdown_all()


def test_exact_pinned_acquisition_never_cold_creates_after_ready_instance_crash(
    tmp_path: Path,
    runtime,
    monkeypatch,
):
    script_path = _write_fake_resident_script(tmp_path)
    snapshot = _toolchain_snapshot(tmp_path, _entrypoint(script_path))
    first_instance = runtime.prewarm(snapshot)
    cached = next(iter(runtime._processes.values()))
    original_begin = cached.begin_request
    original_start = runtime._start_process
    original_acquire = runtime._acquire_process
    starts = [first_instance]
    main_thread = threading.current_thread()
    generic_acquires = []
    crashed = False

    def crash_before_request(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            cached.process.kill()
            cached.process.wait(timeout=2.0)
        return original_begin(*args, **kwargs)

    def record_start(*args, **kwargs):
        created = original_start(*args, **kwargs)
        starts.append(created.instance_id)
        return created

    def record_acquire(*args, **kwargs):
        generic_acquires.append(threading.current_thread() is main_thread)
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(cached, "begin_request", crash_before_request)
    monkeypatch.setattr(runtime, "_start_process", record_start)
    monkeypatch.setattr(runtime, "_acquire_process", record_acquire)

    result = runtime.analyze(
        toolchain_id=snapshot.toolchain_id,
        entrypoint=snapshot.entrypoint,
        package_dir=snapshot.package_dir,
        execution_revision=snapshot.execution_revision,
        required_instance_id=first_instance,
        request_id="pinned-crash",
        request_payload=_request_payload("pinned-crash"),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=2_000,
    )

    assert result["module_status"] == "process_error"
    assert True not in generic_acquires
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline and not runtime.readiness(snapshot):
        time.sleep(0.01)
    assert runtime.readiness(snapshot) is True
    assert runtime.process_instance_id(snapshot) != first_instance
    assert len(starts) == 2


@pytest.mark.parametrize("invalidate", ["stdout", "stderr"])
def test_live_ready_protocol_invalidation_stops_once_before_coalesced_restart(
    tmp_path: Path,
    monkeypatch,
    invalidate,
):
    readiness_events = []
    stopped_instances = []
    runtime = ResidentToolchainRuntime(
        readiness_changed=lambda snapshot, ready: readiness_events.append(
            (snapshot.execution_revision, ready)
        )
    )
    original_stop = runtime._stop_process

    def record_stop(cached_process, **kwargs):
        stopped_instances.append(cached_process.instance_id)
        return original_stop(cached_process, **kwargs)

    monkeypatch.setattr(runtime, "_stop_process", record_stop)
    try:
        script_path = _write_fake_resident_script(tmp_path)
        snapshot = _toolchain_snapshot(tmp_path, _entrypoint(script_path))
        first_instance = runtime.prewarm(snapshot)
        cached = next(iter(runtime._processes.values()))
        if invalidate == "stdout":
            cached.route_stdout_line("idle protocol noise")
        else:
            cached.mark_idle_stderr_invalidated()

        barrier = threading.Barrier(6)
        results = []

        def probe():
            barrier.wait()
            results.append(runtime.readiness(snapshot))

        threads = [threading.Thread(target=probe) for _ in range(5)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2.0)

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not runtime.readiness(snapshot):
            time.sleep(0.01)

        assert results == [False] * 5
        assert cached.process.poll() is not None
        assert stopped_instances.count(first_instance) == 1
        assert runtime.process_instance_id(snapshot) != first_instance
        assert readiness_events.count((snapshot.execution_revision, False)) == 1
        assert readiness_events.count((snapshot.execution_revision, True)) == 2
    finally:
        runtime.shutdown_all()


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


def test_analyze_uses_package_dir_as_process_cwd(
    tmp_path: Path, runtime, monkeypatch
):
    script_path = _write_fake_resident_script(tmp_path)
    package_dir = tmp_path / "package-a"
    other_cwd = tmp_path / "other-cwd"
    package_dir.mkdir()
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    entrypoint = _entrypoint(script_path, "launch-introspect")

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        package_dir=str(package_dir),
        request_id="req-1",
        request_payload=_request_payload("req-1", include_launch_details=True),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=1000,
    )

    ai_result = result["response_payload"]["ai_result"]

    assert result["module_status"] == "success"
    assert ai_result["cwd"] == str(package_dir)


def test_package_dir_change_restarts_cached_process(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    package_dir_a = tmp_path / "package-a"
    package_dir_b = tmp_path / "package-b"
    package_dir_a.mkdir()
    package_dir_b.mkdir()
    entrypoint = _entrypoint(script_path, "launch-introspect")

    first = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        package_dir=str(package_dir_a),
        request_id="req-1",
        request_payload=_request_payload("req-1", include_launch_details=True),
        artifacts_dir=str(tmp_path / "artifacts-1"),
        timeout_ms=1000,
    )
    second = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        package_dir=str(package_dir_b),
        request_id="req-2",
        request_payload=_request_payload("req-2", include_launch_details=True),
        artifacts_dir=str(tmp_path / "artifacts-2"),
        timeout_ms=1000,
    )

    first_ai_result = first["response_payload"]["ai_result"]
    second_ai_result = second["response_payload"]["ai_result"]

    assert first["module_status"] == "success"
    assert second["module_status"] == "success"
    assert first_ai_result["cwd"] == str(package_dir_a)
    assert second_ai_result["cwd"] == str(package_dir_b)
    assert second["process_instance_id"] != first["process_instance_id"]


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific entrypoint parsing")
def test_windows_entrypoint_preserves_embedded_quotes_and_trailing_backslashes(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    expected_args = ["launch-introspect", 'quote"inside', "trail\\"]
    entrypoint = subprocess.list2cmdline(
        [sys.executable, "-u", str(script_path), *expected_args]
    )

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

    assert result["module_status"] == "success"
    assert result["response_payload"]["ai_result"]["argv"] == expected_args


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


@pytest.mark.parametrize(
    ("behavior", "error_fragment"),
    [
        ("unexpected_type", "message_type"),
        ("extra_stdout_during_active_request", "unexpected extra stdout"),
    ],
)
def test_post_response_discard_honors_request_deadline_and_duration(
    tmp_path: Path, runtime, behavior: str, error_fragment: str
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path, "ignore-shutdown")

    started = time.monotonic()
    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1", behavior=behavior),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=100,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    assert result["module_status"] == "protocol_error"
    assert error_fragment in result["error_summary"]
    assert elapsed_ms < 1000
    assert abs(result["duration_ms"] - elapsed_ms) < 300
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


def test_immediate_reuse_fence_timeout_stays_within_request_budget(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)

    warmup = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-warmup",
        request_payload=_request_payload("req-warmup"),
        artifacts_dir=str(tmp_path / "artifacts-warmup"),
        timeout_ms=1000,
    )

    started = time.monotonic()
    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-reuse-timeout",
        request_payload=_request_payload("req-reuse-timeout"),
        artifacts_dir=str(tmp_path / "artifacts-reuse-timeout"),
        timeout_ms=20,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    assert warmup["module_status"] == "success"
    assert warmup["process_instance_id"] is not None
    assert result["module_status"] == "timeout"
    assert result["error_summary"] == "Process timed out"
    assert result["process_instance_id"] == warmup["process_instance_id"]
    assert elapsed_ms < 80, elapsed_ms
    assert result["duration_ms"] < 80
    assert runtime.has_cached_process("tc-1") is False


def test_clipped_reuse_fence_timeout_does_not_capture_previous_late_stderr(
    tmp_path: Path, runtime, monkeypatch
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    original_begin_request = ai_resident_runtime._ResidentProcess.begin_request
    successful_begin_requests = 0

    def delay_second_started_begin_request(self, **kwargs) -> bool:
        nonlocal successful_begin_requests
        started = original_begin_request(self, **kwargs)
        if started:
            successful_begin_requests += 1
            if successful_begin_requests == 2:
                time.sleep(0.25)
        return started

    monkeypatch.setattr(
        ai_resident_runtime._ResidentProcess,
        "begin_request",
        delay_second_started_begin_request,
    )

    first = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            behavior="stderr_after_success",
            late_stderr_delay_s=0.2,
            late_stderr_text="FIRST-LATE|",
        ),
        artifacts_dir=str(tmp_path / "artifacts-1"),
        timeout_ms=1500,
    )

    second = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-2",
        request_payload=_request_payload("req-2"),
        artifacts_dir=str(tmp_path / "artifacts-2"),
        timeout_ms=20,
    )

    assert first["module_status"] == "success"
    assert second["module_status"] == "timeout"
    assert second["error_summary"] == "Process timed out"
    assert second["stderr_excerpt"] == ""
    assert second["process_instance_id"] == first["process_instance_id"]
    assert runtime.has_cached_process("tc-1") is False


def test_timeout_does_not_materially_overrun_request_deadline(
    tmp_path: Path, runtime, monkeypatch
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)

    monkeypatch.setattr(
        ai_resident_runtime._ResidentProcess,
        "_settle_idle_stderr_before_reuse",
        lambda self, **kwargs: False,
    )

    warmup = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-warmup",
        request_payload=_request_payload("req-warmup"),
        artifacts_dir=str(tmp_path / "artifacts-warmup"),
        timeout_ms=1000,
    )

    started = time.monotonic()
    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-timeout",
        request_payload=_request_payload(
            "req-timeout",
            behavior="timeout",
            sleep_s=0.3,
        ),
        artifacts_dir=str(tmp_path / "artifacts-timeout"),
        timeout_ms=20,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    assert warmup["module_status"] == "success"
    assert result["module_status"] == "timeout"
    assert result["error_summary"] == "Process timed out"
    assert elapsed_ms < 55, elapsed_ms
    assert runtime.has_cached_process("tc-1") is False


def test_startup_stderr_before_first_request_does_not_discard_or_leak_into_run(
    tmp_path: Path, runtime, monkeypatch
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    startup_once_file = tmp_path / "startup-stderr-once.txt"
    startup_text = "startup-banner\n"
    original_start_process = ai_resident_runtime.ResidentToolchainRuntime._start_process
    launch_count = 0

    monkeypatch.setenv("RESIDENT_RUNTIME_TEST_STARTUP_STDERR", startup_text)
    monkeypatch.setenv(
        "RESIDENT_RUNTIME_TEST_STARTUP_STDERR_ONCE_FILE",
        str(startup_once_file),
    )

    def count_starts(self, toolchain_id: str, entrypoint: str):
        nonlocal launch_count
        launch_count += 1
        return original_start_process(self, toolchain_id, entrypoint)

    monkeypatch.setattr(
        ai_resident_runtime.ResidentToolchainRuntime,
        "_start_process",
        count_starts,
    )

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1"),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=2000,
    )

    _wait_for_file(startup_once_file)

    assert result["module_status"] == "success"
    assert result["stderr_excerpt"] == ""
    assert launch_count == 1


def test_pre_activation_broken_pipe_preserves_current_run_stderr_excerpt(
    tmp_path: Path, runtime, monkeypatch
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    expected_stderr = "send-window-stderr"

    def fail_after_send_window_stderr(
        self,
        *,
        cached_process,
        request_id: str,
        request_payload: dict,
        artifacts_dir: str,
    ) -> None:
        assert cached_process.ready_to_send() is True
        assert cached_process.mark_request_send_started() is True
        cached_process.mark_request_flushed()
        cached_process.append_stderr(expected_stderr)
        raise BrokenPipeError("simulated broken pipe")

    monkeypatch.setattr(
        ai_resident_runtime.ResidentToolchainRuntime,
        "_send_request",
        fail_after_send_window_stderr,
    )

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1"),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=1000,
    )

    assert result["module_status"] == "process_error"
    assert result["error_summary"] == "simulated broken pipe"
    assert result["stderr_excerpt"] == expected_stderr
    assert runtime.has_cached_process("tc-1") is False


def test_immediate_response_during_send_window_is_not_classified_as_idle_output(
    tmp_path: Path, runtime, monkeypatch
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    original_mark_request_flushed = ai_resident_runtime._ResidentProcess.mark_request_flushed
    original_start_process = ai_resident_runtime.ResidentToolchainRuntime._start_process
    start_count = 0

    def delayed_mark_request_flushed(self) -> None:
        time.sleep(0.2)
        return original_mark_request_flushed(self)

    def count_starts(self, toolchain_id: str, entrypoint: str):
        nonlocal start_count
        start_count += 1
        return original_start_process(self, toolchain_id, entrypoint)

    monkeypatch.setattr(
        ai_resident_runtime._ResidentProcess,
        "mark_request_flushed",
        delayed_mark_request_flushed,
    )
    monkeypatch.setattr(
        ai_resident_runtime.ResidentToolchainRuntime,
        "_start_process",
        count_starts,
    )

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1"),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=1000,
    )

    assert result["module_status"] == "success"
    assert result["response_payload"]["request_id"] == "req-1"
    assert start_count == 1


def test_startup_retry_loop_respects_request_deadline(tmp_path: Path, runtime, monkeypatch):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    original_begin_request = ai_resident_runtime._ResidentProcess.begin_request
    original_discard = ai_resident_runtime.ResidentToolchainRuntime._discard_cached_process
    begin_request_calls = 0
    discard_calls = 0

    def fail_first_begin_request(self, **kwargs) -> bool:
        nonlocal begin_request_calls
        begin_request_calls += 1
        if begin_request_calls == 1:
            return False
        return original_begin_request(self, **kwargs)

    def slow_first_discard(self, toolchain_id: str, cached_process, *args, **kwargs):
        nonlocal discard_calls
        discard_calls += 1
        if discard_calls == 1:
            time.sleep(0.2)
        return original_discard(
            self,
            toolchain_id,
            cached_process,
            *args,
            **kwargs,
        )

    monkeypatch.setattr(
        ai_resident_runtime._ResidentProcess,
        "begin_request",
        fail_first_begin_request,
    )
    monkeypatch.setattr(
        ai_resident_runtime.ResidentToolchainRuntime,
        "_discard_cached_process",
        slow_first_discard,
    )

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload("req-1"),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=100,
    )

    assert result["module_status"] == "timeout"
    assert result["error_summary"] == "Process timed out"
    assert discard_calls == 1


def test_stale_process_cleanup_before_restart_honors_active_request_deadline(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path, "ignore-shutdown")

    first = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            behavior="extra_stdout_after_success",
            extra_stdout_delay_s=0.05,
        ),
        artifacts_dir=str(tmp_path / "artifacts-1"),
        timeout_ms=1000,
    )

    time.sleep(0.2)

    started = time.monotonic()
    second = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-2",
        request_payload=_request_payload("req-2"),
        artifacts_dir=str(tmp_path / "artifacts-2"),
        timeout_ms=100,
    )
    elapsed = time.monotonic() - started

    assert first["module_status"] == "success"
    assert second["module_status"] == "timeout"
    assert second["error_summary"] == "Process timed out"
    assert elapsed < 1.0
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


def test_immediate_second_request_does_not_reuse_process_before_late_idle_stdout(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    started_file = tmp_path / "req-2-started.txt"
    release_file = tmp_path / "req-2-release.txt"

    first = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            behavior="extra_stdout_after_success",
            extra_stdout_delay_s=0.2,
        ),
        artifacts_dir=str(tmp_path / "artifacts-1"),
        timeout_ms=1500,
    )

    def release_second_request() -> None:
        time.sleep(0.35)
        release_file.write_text("release", encoding="utf-8")

    releaser = threading.Thread(target=release_second_request)
    releaser.start()
    second = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-2",
        request_payload=_request_payload(
            "req-2",
            behavior="block",
            started_file=str(started_file),
            release_file=str(release_file),
        ),
        artifacts_dir=str(tmp_path / "artifacts-2"),
        timeout_ms=1500,
    )
    releaser.join(timeout=5)

    assert first["module_status"] == "success"
    assert second["module_status"] == "success"
    assert second["process_instance_id"] != first["process_instance_id"]


def test_idle_stdout_arriving_between_begin_request_and_request_write_discards_before_send(
    tmp_path: Path, runtime, monkeypatch
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    original_send_request = ai_resident_runtime.ResidentToolchainRuntime._send_request
    send_calls = 0

    def inject_stale_stdout_before_write(
        self,
        *,
        cached_process,
        request_id: str,
        request_payload: dict,
        artifacts_dir: str,
    ) -> None:
        nonlocal send_calls
        send_calls += 1
        if send_calls == 2:
            cached_process.route_stdout_line("idle stdout noise")
        return original_send_request(
            self,
            cached_process=cached_process,
            request_id=request_id,
            request_payload=request_payload,
            artifacts_dir=artifacts_dir,
        )

    monkeypatch.setattr(
        ai_resident_runtime.ResidentToolchainRuntime,
        "_send_request",
        inject_stale_stdout_before_write,
    )

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
    assert second["process_instance_id"] != first["process_instance_id"]


def test_idle_extra_stdout_arriving_between_reuse_and_begin_request_discards_before_reuse(
    tmp_path: Path, runtime, monkeypatch
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    original_acquire_process = ai_resident_runtime.ResidentToolchainRuntime._acquire_process
    reuse_gap_opened = threading.Event()
    second_request_started = False

    def delayed_reuse(self, toolchain_id: str, entrypoint: str, **kwargs):
        nonlocal second_request_started
        cached_process, created = original_acquire_process(
            self,
            toolchain_id,
            entrypoint,
            **kwargs,
        )
        if not created and not second_request_started:
            second_request_started = True
            reuse_gap_opened.set()
            time.sleep(0.15)
        return cached_process, created

    monkeypatch.setattr(
        ai_resident_runtime.ResidentToolchainRuntime,
        "_acquire_process",
        delayed_reuse,
    )

    first = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            behavior="extra_stdout_after_success",
            extra_stdout_delay_s=0.05,
        ),
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

    assert reuse_gap_opened.is_set()
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
    assert next(iter(runtime._processes.values())).stderr_buffer_char_count <= 64 * 1024


def test_stderr_buffer_caps_multibyte_utf8_bytes(tmp_path: Path, runtime):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    stderr_text = "界" * 30000

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            stderr_text=stderr_text,
        ),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=2000,
    )

    cached_process = next(iter(runtime._processes.values()))

    assert result["module_status"] == "success"
    assert cached_process.stderr_buffer_byte_count <= 64 * 1024
    assert len(cached_process.stderr_since(0).encode("utf-8")) <= 64 * 1024


def test_stderr_excerpt_keeps_first_2000_chars_even_after_buffer_eviction(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    stderr_text = "".join(f"{index:05d}|" for index in range(15000))

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            stderr_text=stderr_text,
        ),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=2000,
    )

    assert result["module_status"] == "success"
    assert result["stderr_excerpt"] == stderr_text[:2000]


def test_natural_late_stderr_from_previous_request_does_not_leak_into_next_run_excerpt(
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
            behavior="slow_stderr_after_success",
            late_stderr_text="FIRST|",
            late_stderr_repeat=40,
            late_stderr_delay_s=0.01,
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
            stderr_text="SECOND|",
        ),
        artifacts_dir=str(tmp_path / "artifacts-2"),
        timeout_ms=2000,
    )

    assert first["module_status"] == "success"
    assert second["module_status"] == "success"
    assert second["stderr_excerpt"] == "SECOND|"
    assert "FIRST|" not in second["stderr_excerpt"]


def test_immediate_second_request_uses_fresh_process_before_late_idle_stderr(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    started_file = tmp_path / "req-2-started.txt"
    release_file = tmp_path / "req-2-release.txt"
    late_stderr_text = "FIRST-LATE|"

    first = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            behavior="stderr_after_success",
            late_stderr_delay_s=0.2,
            late_stderr_text=late_stderr_text,
        ),
        artifacts_dir=str(tmp_path / "artifacts-1"),
        timeout_ms=1500,
    )

    def release_second_request() -> None:
        time.sleep(0.35)
        release_file.write_text("release", encoding="utf-8")

    releaser = threading.Thread(target=release_second_request)
    releaser.start()
    second = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-2",
        request_payload=_request_payload(
            "req-2",
            behavior="block",
            started_file=str(started_file),
            release_file=str(release_file),
            stderr_text="SECOND|",
        ),
        artifacts_dir=str(tmp_path / "artifacts-2"),
        timeout_ms=1500,
    )
    releaser.join(timeout=5)

    assert first["module_status"] == "success"
    assert second["module_status"] == "success"
    assert second["stderr_excerpt"] == "SECOND|"
    assert late_stderr_text not in second["stderr_excerpt"]
    assert second["process_instance_id"] != first["process_instance_id"]


def test_stale_idle_stderr_backlog_does_not_leak_into_next_run_excerpt(
    tmp_path: Path, runtime, monkeypatch
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    stale_text = "stale-idle-stderr"
    release_stale_append = threading.Event()
    stale_chunk_buffered = threading.Event()
    stale_chunk_appended = threading.Event()
    original_begin_request = ai_resident_runtime._ResidentProcess.begin_request
    original_append_stderr = ai_resident_runtime._ResidentProcess.append_stderr
    original_capture = ai_resident_runtime.ResidentToolchainRuntime._capture_stderr_excerpt
    begin_request_calls = 0
    capture_calls = 0

    def release_on_second_request(self, **kwargs) -> bool:
        nonlocal begin_request_calls
        started = original_begin_request(self, **kwargs)
        if not started:
            return started
        begin_request_calls += 1
        if begin_request_calls == 2:
            release_stale_append.set()
        return started

    def drain_with_buffered_stale_chunk(self, cached_process) -> None:
        try:
            assert cached_process.process.stderr is not None
            if not stale_chunk_buffered.is_set():
                stale_chunk = cached_process.process.stderr.read(len(stale_text))
                if stale_chunk:
                    stale_start, _ = cached_process.reserve_stderr_chunk(stale_chunk)
                    stale_chunk_buffered.set()
                    assert release_stale_append.wait(timeout=2)
                    original_append_stderr(
                        cached_process,
                        stale_chunk,
                        start_position=stale_start,
                    )
                    stale_chunk_appended.set()
            while True:
                chunk = cached_process.process.stderr.read(1)
                if not chunk:
                    break
                chunk_start, _ = cached_process.reserve_stderr_chunk(chunk)
                original_append_stderr(
                    cached_process,
                    chunk,
                    start_position=chunk_start,
                )
        except Exception:
            return

    def capture_after_stale_append(self, cached_process, *, deadline=None):
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 2:
            assert stale_chunk_appended.wait(timeout=2)
            time.sleep(0.05)
        return original_capture(self, cached_process, deadline=deadline)

    monkeypatch.setattr(
        ai_resident_runtime._ResidentProcess,
        "begin_request",
        release_on_second_request,
    )
    monkeypatch.setattr(
        ai_resident_runtime.ResidentToolchainRuntime,
        "_drain_stderr",
        drain_with_buffered_stale_chunk,
    )
    monkeypatch.setattr(
        ai_resident_runtime.ResidentToolchainRuntime,
        "_capture_stderr_excerpt",
        capture_after_stale_append,
    )

    first = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            behavior="stderr_after_success",
            late_stderr_text=stale_text,
        ),
        artifacts_dir=str(tmp_path / "artifacts-1"),
        timeout_ms=1000,
    )
    assert stale_chunk_buffered.wait(timeout=2)

    second = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-2",
        request_payload=_request_payload(
            "req-2",
            stderr_text="fresh-run-stderr",
        ),
        artifacts_dir=str(tmp_path / "artifacts-2"),
        timeout_ms=1000,
    )

    assert first["module_status"] == "success"
    assert second["module_status"] == "success"
    assert second["stderr_excerpt"] == "fresh-run-stderr"


def test_extra_stdout_after_first_response_line_fails_current_run_with_protocol_error(
    tmp_path: Path, runtime, monkeypatch
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)
    release_file = tmp_path / "release-extra-stdout.txt"
    success_commit_opened = threading.Event()
    original_build_result = ai_resident_runtime.ResidentToolchainRuntime._build_result

    def delayed_build_result(*, status, **kwargs):
        if status == "success":
            success_commit_opened.set()
            release_file.write_text("release", encoding="utf-8")
            time.sleep(0.05)
        return original_build_result(status=status, **kwargs)

    monkeypatch.setattr(
        ai_resident_runtime.ResidentToolchainRuntime,
        "_build_result",
        staticmethod(delayed_build_result),
    )

    result = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-1",
        request_payload=_request_payload(
            "req-1",
            behavior="extra_stdout_after_gate",
            extra_stdout_release_file=str(release_file),
        ),
        artifacts_dir=str(tmp_path / "artifacts"),
        timeout_ms=1000,
    )

    assert success_commit_opened.is_set()
    assert result["module_status"] == "protocol_error"
    assert "unexpected extra stdout" in result["error_summary"]
    assert runtime.has_cached_process("tc-1") is False


def test_mutation_lease_blocks_analyze_until_mutation_finishes(
    tmp_path: Path, runtime
):
    script_path = _write_fake_resident_script(tmp_path)
    entrypoint = _entrypoint(script_path)

    with runtime.hold_mutation_lease("tc-1"):
        blocked = runtime.analyze(
            toolchain_id="tc-1",
            entrypoint=entrypoint,
            request_id="req-blocked",
            request_payload=_request_payload("req-blocked"),
            artifacts_dir=str(tmp_path / "artifacts-blocked"),
            timeout_ms=1000,
        )

        assert blocked["module_status"] == "process_error"
        assert blocked["error_summary"] == "Resident toolchain is busy"

    allowed = runtime.analyze(
        toolchain_id="tc-1",
        entrypoint=entrypoint,
        request_id="req-allowed",
        request_payload=_request_payload("req-allowed"),
        artifacts_dir=str(tmp_path / "artifacts-allowed"),
        timeout_ms=1000,
    )

    assert allowed["module_status"] == "success"


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
    cached_process = next(iter(runtime._processes.values())).process

    started = time.monotonic()
    runtime.shutdown_all()
    elapsed = time.monotonic() - started

    assert first["module_status"] == "success"
    assert shutdown_marker.read_text(encoding="utf-8") == "shutdown"
    assert cached_process.poll() is not None
    assert elapsed >= 1.8
    assert runtime.has_cached_process("tc-1") is False
