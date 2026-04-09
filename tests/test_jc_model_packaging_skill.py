import importlib.util
import io
import json
from pathlib import Path

import pytest


ROOT = Path("jc-model-packaging-skill")

OWNED_REQUIRED_FILES = [
    ROOT / "SKILL.md",
    ROOT / "templates" / "metadata" / "README.md",
    ROOT / "templates" / "metadata" / "model_contract.template.json",
    ROOT / "templates" / "metadata" / "packaging_decision_checklist.md",
    ROOT / "templates" / "python_oneshot_cli" / "README.md",
    ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py",
    ROOT / "templates" / "python_oneshot_cli" / "protocol_contract.md",
]

RESIDENT_REQUIRED_FILES = [
    ROOT / "templates" / "python_resident_stdio" / "README.md",
    ROOT / "templates" / "python_resident_stdio" / "resident_cli.py",
    ROOT / "templates" / "python_resident_stdio" / "resident_protocol.md",
]

LATER_SLICE_FILES = [
    ROOT / "templates" / "pyinstaller_windows" / "README.md",
    ROOT / "templates" / "pyinstaller_windows" / "build.ps1",
    ROOT / "templates" / "pyinstaller_windows" / "toolchain_oneshot.spec",
    ROOT / "templates" / "pyinstaller_windows" / "toolchain_resident.spec",
    ROOT / "templates" / "smoke_test" / "README.md",
    ROOT / "templates" / "smoke_test" / "request.json",
    ROOT / "templates" / "smoke_test" / "response.success.json",
    ROOT / "templates" / "smoke_test" / "stderr.error.txt",
    ROOT / "templates" / "smoke_test" / "resident_input.jsonl",
    ROOT / "templates" / "smoke_test" / "resident_shutdown_input.jsonl",
    ROOT / "templates" / "smoke_test" / "resident_output.success.jsonl",
    ROOT / "templates" / "smoke_test" / "resident_output.error.jsonl",
]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_request_payload(audio_path: str) -> dict:
    return {
        "protocol_version": 1,
        "request_id": "req_123",
        "test_run_id": "run_123",
        "toolchain_id": "toolchain_123",
        "segments": [
            {
                "segment_id": "segment_0",
                "role": "device_1",
                "audio_path": audio_path,
                "channels": [0],
                "duration_ms": 4000,
                "sample_rate": 44100,
                "metadata": {},
            },
            {
                "segment_id": "segment_1",
                "role": "device_2",
                "audio_path": audio_path,
                "channels": [0],
                "duration_ms": 4000,
                "sample_rate": 44100,
                "metadata": {},
            },
        ],
        "context": {},
        "params": {},
        "window_info": {},
    }


def _resident_analyze_envelope(tmp_path: Path, request_id: str = "req_123") -> dict:
    request_payload = _valid_request_payload(str(tmp_path / "input.wav"))
    request_payload["request_id"] = request_id
    return {
        "message_type": "analyze",
        "request_id": request_id,
        "request": request_payload,
        "artifacts_dir": str(tmp_path / "artifacts"),
    }


def _resident_jsonl(*payloads: dict) -> str:
    return "".join(json.dumps(payload) + "\n" for payload in payloads)


def _run_resident_main(
    module,
    monkeypatch: pytest.MonkeyPatch,
    stdin_text: str,
    *,
    stdout: io.TextIOBase | None = None,
    stderr: io.TextIOBase | None = None,
) -> tuple[int, str, str]:
    stdin = io.StringIO(stdin_text)
    stdout = stdout or io.StringIO()
    stderr = stderr or io.StringIO()
    monkeypatch.setattr(module.sys, "stdin", stdin)
    monkeypatch.setattr(module.sys, "stdout", stdout)
    monkeypatch.setattr(module.sys, "stderr", stderr)
    exit_code = module.main()
    return exit_code, stdout.getvalue(), stderr.getvalue()


def test_skill_package_contains_slice_one_required_files():
    missing = [str(path) for path in OWNED_REQUIRED_FILES if not path.is_file()]
    assert missing == []


def test_skill_package_contains_slice_two_resident_files():
    missing = [str(path) for path in RESIDENT_REQUIRED_FILES if not path.is_file()]
    assert missing == []


@pytest.mark.xfail(
    reason="Windows packaging and smoke fixtures land in later slices."
)
def test_future_skill_package_files_are_added_in_later_slices():
    missing = [str(path) for path in LATER_SLICE_FILES if not path.is_file()]
    assert missing == []


def test_owned_skill_pack_text_is_self_contained_and_mentions_required_modes():
    text_files = [path for path in OWNED_REQUIRED_FILES if path.suffix in {".md", ".py", ".json"}]
    combined = "\n".join(_read_text(path) for path in text_files).lower()
    assert "one-shot" in combined
    assert "resident" in combined
    assert "duration" in combined
    forbidden = ["docs/double-sdd", "app/", "ui/", "jingchengmechanical"]
    assert all(token not in combined for token in forbidden)


def test_skill_guidance_locks_decisions_and_contract_rules():
    skill_text = _read_text(ROOT / "SKILL.md")
    assert "--request" in skill_text
    assert "--response" in skill_text
    assert "--artifacts-dir" in skill_text
    assert "one-shot" in skill_text.lower()
    assert "resident" in skill_text.lower()
    assert "duration mismatch" in skill_text.lower()
    assert "segment_index" in skill_text
    assert "segments[0]" in skill_text
    assert "relative_path" in skill_text


def test_skill_guidance_explains_when_to_keep_weights_external_or_embedded():
    skill_text = _read_text(ROOT / "SKILL.md").lower()
    assert "keep weights external when" in skill_text
    assert "embed weights when" in skill_text


def test_skill_guidance_recommends_resident_mode_for_expensive_imports_and_reuse():
    skill_text = _read_text(ROOT / "SKILL.md").lower()
    assert "choose the resident template when" in skill_text
    assert "imports are expensive" in skill_text
    assert "stay in memory" in skill_text
    assert "repeated requests" in skill_text


def test_metadata_templates_capture_duration_contract_and_packaging_choices():
    metadata_readme = _read_text(ROOT / "templates" / "metadata" / "README.md").lower()
    contract_template = _read_text(
        ROOT / "templates" / "metadata" / "model_contract.template.json"
    ).lower()
    checklist = _read_text(
        ROOT / "templates" / "metadata" / "packaging_decision_checklist.md"
    ).lower()

    assert "sample rate" in metadata_readme
    assert "tensor shape" in metadata_readme
    assert "normalization" in metadata_readme
    assert "output interpretation" in metadata_readme

    assert '"duration_policy"' in contract_template
    assert '"target_num_samples"' in contract_template
    assert '"preprocessing_steps"' in contract_template

    assert "one-shot" in checklist
    assert "resident" in checklist
    assert ".exe" in checklist
    assert "trim / pad" in checklist
    assert "sliding window" in checklist
    assert "strict reject" in checklist
    assert "variable-length" in checklist
    assert "external" in checklist
    assert "embed" in checklist


def test_oneshot_template_mentions_required_cli_flags_and_hooks():
    text = _read_text(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    assert "--request" in text
    assert "--response" in text
    assert "--artifacts-dir" in text
    assert "def load_request" in text
    assert "def preprocess_from_request" in text
    assert "def run_model" in text
    assert "def write_artifacts" in text
    assert "def build_success_response" in text
    assert "stderr" in text.lower()
    assert "exit 1" in text.lower() or "sys.exit(1)" in text.lower()


def test_oneshot_contract_locks_absolute_paths_segment_fallback_and_cleanup():
    text = _read_text(ROOT / "templates" / "python_oneshot_cli" / "protocol_contract.md")
    lowered = text.lower()
    assert "--request" in text
    assert "--response" in text
    assert "--artifacts-dir" in text
    assert "absolute" in lowered
    assert "segment_index" in text
    assert "segments[0]" in text
    assert "relative_path" in text
    assert "stale" in lowered
    assert "fatal" in lowered


def test_oneshot_contract_defines_artifact_type_as_descriptive_metadata():
    text = _read_text(ROOT / "templates" / "python_oneshot_cli" / "protocol_contract.md")
    lowered = text.lower()
    assert "descriptive metadata" in lowered
    assert "not a host-parsed enum" in lowered
    assert "report_json" in text
    assert "spectrogram_npy" in text
    assert "image_png" in text
    assert "audio_wav" in text


def test_oneshot_template_omits_optional_numeric_fields_by_default():
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    response = module.build_success_response(
        request_payload={"request_id": "req_123"},
        inference_output={"raw_score": 0.42},
        artifacts=[],
    )

    assert response["request_id"] == "req_123"
    assert response["ai_result"]["ai_label"] == "PASS"
    assert "score" not in response["ai_result"]
    assert "confidence" not in response["ai_result"]


@pytest.mark.parametrize(
    ("flag_name", "invalid_value"),
    [
        ("--request", "relative-request.json"),
        ("--response", "relative-response.json"),
        ("--artifacts-dir", "relative-artifacts"),
    ],
)
def test_oneshot_main_routes_invalid_cli_paths_through_fatal_handler(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag_name: str,
    invalid_value: str,
):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    request_path = tmp_path / "request.json"
    request_path.write_text(
        '{"protocol_version": 1, "request_id": "req_123", "test_run_id": "run_123", '
        '"toolchain_id": "toolchain_123", "segments": [{"segment_id": "segment_0", '
        '"role": "device_1", "audio_path": "'
        + str(tmp_path / "input.wav").replace("\\", "/")
        + '", "channels": [0], "duration_ms": 4000, "sample_rate": 44100, "metadata": {}}], '
        '"context": {}, "params": {}, "window_info": {}}',
        encoding="utf-8",
    )
    response_path = tmp_path / "response.json"
    response_path.write_text("stale", encoding="utf-8")
    artifacts_dir = tmp_path / "artifacts"
    args = {
        "--request": str(request_path),
        "--response": str(response_path),
        "--artifacts-dir": str(artifacts_dir),
    }
    args[flag_name] = invalid_value

    exit_code = module.main(
        [
            "--request",
            args["--request"],
            "--response",
            args["--response"],
            "--artifacts-dir",
            args["--artifacts-dir"],
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Fatal one-shot failure:" in captured.err
    assert flag_name in captured.err
    assert captured.out == ""
    if flag_name != "--response":
        assert not response_path.exists()


def test_oneshot_main_preserves_original_fatal_error_when_response_is_an_existing_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    response_dir = tmp_path / "response-dir"
    response_dir.mkdir()

    exit_code = module.main(
        [
            "--request",
            "relative-request.json",
            "--response",
            str(response_dir),
            "--artifacts-dir",
            str(tmp_path / "artifacts"),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "Fatal one-shot failure: --request must be an absolute path.\n"
    assert response_dir.is_dir()


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "expected_message"),
    [
        ("segment_id", 123, "segments[0].segment_id must be a string."),
        ("role", 123, "segments[0].role must be a string."),
        ("channels", "0", "segments[0].channels must be a list."),
        ("duration_ms", "4000", "segments[0].duration_ms must be an integer."),
        ("sample_rate", 44100.0, "segments[0].sample_rate must be an integer."),
        ("metadata", [], "segments[0].metadata must be an object."),
    ],
)
def test_load_request_rejects_invalid_segment_schema_fields(
    tmp_path: Path, field_name: str, invalid_value: object, expected_message: str
):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    request_payload = _valid_request_payload(str(tmp_path / "input.wav"))
    request_payload["segments"][0][field_name] = invalid_value
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request_payload), encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        module.load_request(str(request_path))

    assert str(excinfo.value) == expected_message


def test_load_request_rejects_boolean_protocol_version(tmp_path: Path):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    request_payload = _valid_request_payload(str(tmp_path / "input.wav"))
    request_payload["protocol_version"] = True
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="protocol_version must equal 1."):
        module.load_request(str(request_path))


@pytest.mark.parametrize("segment_index", [True, False, -1, "1", 5, None])
def test_select_segment_falls_back_to_first_segment_for_invalid_index_values(
    tmp_path: Path, segment_index: object
):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    audio_path = str(tmp_path / "input.wav")
    request_payload = _valid_request_payload(audio_path)
    request_payload["params"]["segment_index"] = segment_index

    selected = module.select_segment(request_payload)

    assert selected["segment_id"] == "segment_0"


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        ({"type": "", "relative_path": "report.json"}, "Artifact type"),
        ({"type": "   ", "relative_path": "report.json"}, "Artifact type"),
        ({"type": "report_json", "relative_path": ""}, "Artifact relative_path"),
        ({"type": "report_json", "relative_path": "   "}, "Artifact relative_path"),
    ],
)
def test_build_success_response_rejects_invalid_artifact_metadata(
    artifact: dict[str, str], message: str
):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")

    with pytest.raises(ValueError, match=message):
        module.build_success_response(
            request_payload={"request_id": "req_123"},
            inference_output={"raw_score": 0.42},
            artifacts=[artifact],
        )


@pytest.mark.parametrize("relative_path", ["C:report.json", "/report.json", "\\report.json"])
def test_build_success_response_rejects_windows_anchored_artifact_paths(
    tmp_path: Path, relative_path: str
):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    with pytest.raises(ValueError, match="Artifact relative_path must stay relative."):
        module.build_success_response(
            request_payload={"request_id": "req_123"},
            inference_output={"raw_score": 0.42},
            artifacts=[{"type": "report_json", "relative_path": relative_path}],
            artifacts_dir=artifacts_dir,
        )


def test_build_success_response_rejects_missing_declared_artifact(tmp_path: Path):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    with pytest.raises(
        ValueError,
        match="Artifact relative_path must reference an existing readable file under artifacts_dir.",
    ):
        module.build_success_response(
            request_payload={"request_id": "req_123"},
            inference_output={"raw_score": 0.42},
            artifacts=[{"type": "report_json", "relative_path": "reports/report.json"}],
            artifacts_dir=artifacts_dir,
        )


def test_build_success_response_rejects_declared_artifact_directory(tmp_path: Path):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    artifacts_dir = tmp_path / "artifacts"
    artifact_dir = artifacts_dir / "reports"
    artifact_dir.mkdir(parents=True)

    with pytest.raises(
        ValueError,
        match="Artifact relative_path must reference an existing readable file under artifacts_dir.",
    ):
        module.build_success_response(
            request_payload={"request_id": "req_123"},
            inference_output={"raw_score": 0.42},
            artifacts=[{"type": "report_json", "relative_path": "reports"}],
            artifacts_dir=artifacts_dir,
        )


def test_build_success_response_default_payload_stays_json_safe(tmp_path: Path):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    response = module.build_success_response(
        request_payload={"request_id": "req_123"},
        inference_output=object(),
        artifacts=[],
    )
    response_path = tmp_path / "response.json"

    module.write_response(response_path, response)

    written = json.loads(response_path.read_text(encoding="utf-8"))
    assert written["ai_result"]["extra"] == {}


def test_cleanup_stale_response_retries_after_read_only_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    response_path = tmp_path / "response.json"
    response_path.write_text("stale", encoding="utf-8")
    original_unlink = module.Path.unlink
    unlink_calls: list[Path] = []
    chmod_calls: list[tuple[Path, int]] = []

    def flaky_unlink(self: Path, *args, **kwargs):
        unlink_calls.append(self)
        if self == response_path and len(unlink_calls) == 1:
            raise PermissionError("read-only")
        return original_unlink(self, *args, **kwargs)

    def tracking_chmod(self: Path, mode: int):
        chmod_calls.append((self, mode))

    monkeypatch.setattr(module.Path, "unlink", flaky_unlink)
    monkeypatch.setattr(module.Path, "chmod", tracking_chmod)

    module.cleanup_stale_response(response_path)

    assert len(unlink_calls) == 2
    assert len(chmod_calls) == 1
    assert chmod_calls[0][0] == response_path
    assert not response_path.exists()


def test_oneshot_main_surfaces_stale_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    module = _load_module(ROOT / "templates" / "python_oneshot_cli" / "toolchain_cli.py")
    response_path = tmp_path / "response.json"
    response_path.write_text("stale", encoding="utf-8")

    def fail_cleanup(path: Path) -> None:
        raise RuntimeError(f"Unable to remove stale response file at {path}.")

    monkeypatch.setattr(module, "cleanup_stale_response", fail_cleanup)

    exit_code = module.main(
        [
            "--request",
            "relative-request.json",
            "--response",
            str(response_path),
            "--artifacts-dir",
            str(tmp_path / "artifacts"),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Fatal one-shot failure: --request must be an absolute path." in captured.err
    assert "Unable to remove stale response file" in captured.err
    assert response_path.exists()


def test_resident_template_mentions_protocol_envelopes():
    text = _read_text(ROOT / "templates" / "python_resident_stdio" / "resident_protocol.md")
    assert '"message_type": "analyze"' in text
    assert '"message_type": "result"' in text
    assert '"message_type": "error"' in text
    assert '"message_type": "shutdown"' in text
    assert '"request_id": "req_20260409_120000_abcd1234"' in text
    assert '"artifacts_dir": "D:/absolute/path/artifacts"' in text


def test_resident_contract_locks_request_matching_and_error_lifecycle():
    text = _read_text(ROOT / "templates" / "python_resident_stdio" / "resident_protocol.md")
    lowered = text.lower()
    assert "utf-8" in lowered
    assert "stdin" in lowered and "stdout" in lowered
    assert "request.request_id" in text
    assert "must equal outer `request_id`" in text or "inner and outer" in lowered
    assert "artifacts_dir" in text and "absolute path" in lowered
    assert "request.params.segment_index" in text
    assert "request.segments[0]" in text
    assert "stderr" in lowered
    assert "non-zero" in lowered
    assert "preload_model" in text
    assert "process-global runtime initialization" in lowered
    assert "no `error` envelope" in text or "no error envelope" in lowered
    assert "continue accepting later `analyze` messages" in text
    assert "external host may still choose to discard the process" in lowered
    assert "no extra stdout" in lowered or "no unsolicited stdout" in lowered
    assert "type` must be a non-empty string" in text
    assert "relative_path` must be a non-empty string" in text
    assert "report_json" in text
    assert "spectrogram_npy" in text
    assert "image_png" in text
    assert "audio_wav" in text
    assert "must not contain `..`" in text
    assert "must already exist under the supplied artifacts directory" in lowered
    assert "must never reference files outside the supplied artifacts directory" in lowered


def test_resident_template_mentions_required_hooks():
    text = _read_text(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    assert (
        "def handle_analyze(request_id: str, request_payload: dict, artifacts_dir: str) -> dict:"
        in text
    )
    assert "for line in sys.stdin" in text
    assert "json.loads(line)" in text
    assert "preload_model" in text
    assert "cache" in text.lower()
    assert "error" in text.lower()
    assert "shutdown" in text.lower()
    assert "utf-8" in text.lower()
    assert "report_json" in text
    assert "spectrogram_npy" in text
    assert "image_png" in text
    assert "audio_wav" in text
    assert "relative_path" in text
    assert "must never reference files outside the supplied artifacts directory" in text.lower()


def test_resident_readme_mentions_utf8_jsonl_transport():
    text = _read_text(ROOT / "templates" / "python_resident_stdio" / "README.md").lower()
    assert "utf-8" in text
    assert "json lines" in text or "jsonl" in text
    assert "stdin" in text and "stdout" in text
    assert "preload" in text
    assert "fatal" in text


def test_resident_main_exits_non_zero_for_malformed_protocol_envelope(
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl({"message_type": "analyze", "request_id": "req_123"}),
    )

    assert exit_code == 1
    assert stdout_text == ""
    assert "Malformed resident protocol message: analyze requires object request." in stderr_text


def test_resident_main_shutdown_emits_no_stdout(monkeypatch: pytest.MonkeyPatch):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl({"message_type": "shutdown"}),
    )

    assert exit_code == 0
    assert stdout_text == ""
    assert stderr_text == ""


def test_resident_main_rejects_mismatched_inner_and_outer_request_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    envelope = _resident_analyze_envelope(tmp_path, request_id="req_outer")
    envelope["request"]["request_id"] = "req_inner"

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl(envelope),
    )

    assert exit_code == 1
    assert stdout_text == ""
    assert "request.request_id must equal outer request_id." in stderr_text


def test_resident_main_falls_back_to_first_segment_for_invalid_segment_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    envelope = _resident_analyze_envelope(tmp_path)
    envelope["request"]["params"]["segment_index"] = 99
    selected_segment_ids: list[str] = []

    monkeypatch.setattr(module, "preload_model", lambda: "model")
    monkeypatch.setattr(module, "get_process_cache", lambda: {})

    def preprocess_from_request(request_payload, selected_segment, cache):
        _ = (request_payload, cache)
        selected_segment_ids.append(selected_segment["segment_id"])
        return {"selected_segment_id": selected_segment["segment_id"]}

    def run_model(model_handle, preprocessed, cache):
        _ = (model_handle, cache)
        return preprocessed

    def write_artifacts(artifacts_dir, inference_output, cache):
        _ = (inference_output, cache)
        report_path = Path(artifacts_dir) / "report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        return [{"type": "report_json", "relative_path": "report.json"}]

    monkeypatch.setattr(module, "preprocess_from_request", preprocess_from_request)
    monkeypatch.setattr(module, "run_model", run_model)
    monkeypatch.setattr(module, "write_artifacts", write_artifacts)

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl(envelope, {"message_type": "shutdown"}),
    )

    assert exit_code == 0
    assert stderr_text == ""
    assert selected_segment_ids == ["segment_0"]
    assert json.loads(stdout_text)["message_type"] == "result"


def test_resident_main_continues_after_request_scoped_error_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    first_envelope = _resident_analyze_envelope(tmp_path, request_id="req_fail")
    second_envelope = _resident_analyze_envelope(tmp_path, request_id="req_ok")
    call_count = {"count": 0}

    monkeypatch.setattr(module, "preload_model", lambda: "model")
    monkeypatch.setattr(module, "get_process_cache", lambda: {})

    def preprocess_from_request(request_payload, selected_segment, cache):
        _ = (request_payload, selected_segment, cache)
        call_count["count"] += 1
        if call_count["count"] == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    def run_model(model_handle, preprocessed, cache):
        _ = (model_handle, cache)
        return preprocessed

    def write_artifacts(artifacts_dir, inference_output, cache):
        _ = (inference_output, cache)
        report_path = Path(artifacts_dir) / "report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        return [{"type": "report_json", "relative_path": "report.json"}]

    monkeypatch.setattr(module, "preprocess_from_request", preprocess_from_request)
    monkeypatch.setattr(module, "run_model", run_model)
    monkeypatch.setattr(module, "write_artifacts", write_artifacts)

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl(first_envelope, second_envelope, {"message_type": "shutdown"}),
    )

    assert exit_code == 0
    assert stderr_text == ""
    lines = stdout_text.strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {
        "message_type": "error",
        "request_id": "req_fail",
        "error": "Request failed: boom",
    }
    assert json.loads(lines[1])["request_id"] == "req_ok"
    assert json.loads(lines[1])["message_type"] == "result"


def test_resident_main_initializes_runtime_once_per_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    first_envelope = _resident_analyze_envelope(tmp_path, request_id="req_one")
    second_envelope = _resident_analyze_envelope(tmp_path, request_id="req_two")
    preload_calls = {"count": 0}
    cache_calls = {"count": 0}
    cache = {"hits": 0}

    def preload_model():
        preload_calls["count"] += 1
        return "model"

    def get_process_cache():
        cache_calls["count"] += 1
        return cache

    def preprocess_from_request(request_payload, selected_segment, active_cache):
        _ = (request_payload, selected_segment)
        active_cache["hits"] += 1
        return {"hits": active_cache["hits"]}

    def run_model(model_handle, preprocessed, active_cache):
        _ = (model_handle, active_cache)
        return preprocessed

    def write_artifacts(artifacts_dir, inference_output, active_cache):
        _ = (inference_output, active_cache)
        report_path = Path(artifacts_dir) / "report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        return [{"type": "report_json", "relative_path": "report.json"}]

    monkeypatch.setattr(module, "preload_model", preload_model)
    monkeypatch.setattr(module, "get_process_cache", get_process_cache)
    monkeypatch.setattr(module, "preprocess_from_request", preprocess_from_request)
    monkeypatch.setattr(module, "run_model", run_model)
    monkeypatch.setattr(module, "write_artifacts", write_artifacts)

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl(first_envelope, second_envelope, {"message_type": "shutdown"}),
    )

    assert exit_code == 0
    assert stderr_text == ""
    assert preload_calls["count"] == 1
    assert cache_calls["count"] == 1
    lines = [json.loads(line) for line in stdout_text.strip().splitlines()]
    assert [line["request_id"] for line in lines] == ["req_one", "req_two"]


def test_resident_main_emits_error_envelope_when_declared_artifact_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    envelope = _resident_analyze_envelope(tmp_path)

    monkeypatch.setattr(module, "preload_model", lambda: "model")
    monkeypatch.setattr(module, "get_process_cache", lambda: {})
    monkeypatch.setattr(
        module,
        "preprocess_from_request",
        lambda request_payload, selected_segment, cache: {"ok": True},
    )
    monkeypatch.setattr(
        module,
        "run_model",
        lambda model_handle, preprocessed, cache: {"ok": True},
    )
    monkeypatch.setattr(
        module,
        "write_artifacts",
        lambda artifacts_dir, inference_output, cache: [
            {"type": "report_json", "relative_path": "missing/report.json"}
        ],
    )

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl(envelope, {"message_type": "shutdown"}),
    )

    assert exit_code == 0
    assert stderr_text == ""
    assert json.loads(stdout_text) == {
        "message_type": "error",
        "request_id": "req_123",
        "error": (
            "Request failed: Artifact relative_path must reference an existing readable "
            "file under artifacts_dir."
        ),
    }


def test_resident_main_emits_request_scoped_error_when_result_payload_is_not_json_serializable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    first_envelope = _resident_analyze_envelope(tmp_path, request_id="req_bad")
    second_envelope = _resident_analyze_envelope(tmp_path, request_id="req_ok")

    monkeypatch.setattr(module, "preload_model", lambda: "model")
    monkeypatch.setattr(module, "get_process_cache", lambda: {})
    monkeypatch.setattr(
        module,
        "preprocess_from_request",
        lambda request_payload, selected_segment, cache: {"ok": True},
    )
    monkeypatch.setattr(
        module,
        "run_model",
        lambda model_handle, preprocessed, cache: {"ok": True},
    )

    def build_result_envelope(request_id, inference_output, artifacts, artifacts_dir):
        _ = (inference_output, artifacts, artifacts_dir)
        if request_id == "req_bad":
            return {
                "message_type": "result",
                "request_id": request_id,
                "response": {
                    "protocol_version": 1,
                    "request_id": request_id,
                    "toolchain_name": "Toolchain",
                    "toolchain_version": "1.0.0",
                    "ai_result": {
                        "ai_label": "PASS",
                        "extra": {"raw": object()},
                    },
                },
            }
        return {
            "message_type": "result",
            "request_id": request_id,
            "response": {
                "protocol_version": 1,
                "request_id": request_id,
                "toolchain_name": "Toolchain",
                "toolchain_version": "1.0.0",
                "ai_result": {
                    "ai_label": "PASS",
                    "extra": {},
                },
            },
        }

    monkeypatch.setattr(module, "write_artifacts", lambda artifacts_dir, inference_output, cache: [])
    monkeypatch.setattr(module, "build_result_envelope", build_result_envelope)

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl(first_envelope, second_envelope, {"message_type": "shutdown"}),
    )

    assert exit_code == 0
    assert stderr_text == ""
    lines = [json.loads(line) for line in stdout_text.strip().splitlines()]
    assert lines == [
        {
            "message_type": "error",
            "request_id": "req_bad",
            "error": "Request failed: Protocol output is not JSON serializable: Object of type object is not JSON serializable",
        },
        {
            "message_type": "result",
            "request_id": "req_ok",
            "response": {
                "protocol_version": 1,
                "request_id": "req_ok",
                "toolchain_name": "Toolchain",
                "toolchain_version": "1.0.0",
                "ai_result": {
                    "ai_label": "PASS",
                    "extra": {},
                },
            },
        },
    ]


def test_resident_main_falls_back_to_fatal_exit_when_serialization_error_envelope_cannot_be_emitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    envelope = _resident_analyze_envelope(tmp_path, request_id="req_bad")
    shutdown_calls: list[str] = []

    monkeypatch.setattr(module, "preload_model", lambda: "model")
    monkeypatch.setattr(module, "get_process_cache", lambda: {})
    monkeypatch.setattr(
        module,
        "preprocess_from_request",
        lambda request_payload, selected_segment, cache: {"ok": True},
    )
    monkeypatch.setattr(
        module,
        "run_model",
        lambda model_handle, preprocessed, cache: {"ok": True},
    )
    monkeypatch.setattr(module, "write_artifacts", lambda artifacts_dir, inference_output, cache: [])
    monkeypatch.setattr(
        module,
        "build_result_envelope",
        lambda request_id, inference_output, artifacts, artifacts_dir: {
            "message_type": "result",
            "request_id": request_id,
            "response": {"ai_result": {"extra": {"raw": object()}}},
        },
    )
    monkeypatch.setattr(
        module,
        "build_error_envelope",
        lambda request_id, error_message: {
            "message_type": "error",
            "request_id": request_id,
            "error": object(),
        },
    )
    monkeypatch.setattr(module, "shutdown_runtime", lambda: shutdown_calls.append("shutdown"))

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl(envelope),
    )

    assert exit_code == 1
    assert stdout_text == ""
    assert (
        stderr_text
        == "Fatal resident protocol failure: Protocol output is not JSON serializable: Object of type object is not JSON serializable\n"
    )
    assert shutdown_calls == ["shutdown"]


def test_resident_main_treats_stdout_failure_during_result_emission_as_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    envelope = _resident_analyze_envelope(tmp_path)
    shutdown_calls: list[str] = []

    class BrokenStdout(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError("stdout transport failed during result emission")

    monkeypatch.setattr(module, "preload_model", lambda: "model")
    monkeypatch.setattr(module, "get_process_cache", lambda: {})
    monkeypatch.setattr(
        module,
        "preprocess_from_request",
        lambda request_payload, selected_segment, cache: {"ok": True},
    )
    monkeypatch.setattr(
        module,
        "run_model",
        lambda model_handle, preprocessed, cache: {"ok": True},
    )

    def write_artifacts(artifacts_dir, inference_output, cache):
        _ = (inference_output, cache)
        report_path = Path(artifacts_dir) / "report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        return [{"type": "report_json", "relative_path": "report.json"}]

    monkeypatch.setattr(module, "write_artifacts", write_artifacts)
    monkeypatch.setattr(module, "shutdown_runtime", lambda: shutdown_calls.append("shutdown"))

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl(envelope),
        stdout=BrokenStdout(),
    )

    assert exit_code == 1
    assert stdout_text == ""
    assert "Fatal resident protocol failure:" in stderr_text
    assert "stdout transport failed during result emission" in stderr_text
    assert shutdown_calls == ["shutdown"]


def test_resident_main_treats_stdout_failure_during_error_emission_as_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    envelope = _resident_analyze_envelope(tmp_path)
    shutdown_calls: list[str] = []

    class BrokenStdout(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError("stdout transport failed during error emission")

    monkeypatch.setattr(module, "preload_model", lambda: "model")
    monkeypatch.setattr(module, "get_process_cache", lambda: {})

    def preprocess_from_request(request_payload, selected_segment, cache):
        _ = (request_payload, selected_segment, cache)
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "preprocess_from_request", preprocess_from_request)
    monkeypatch.setattr(module, "shutdown_runtime", lambda: shutdown_calls.append("shutdown"))

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl(envelope),
        stdout=BrokenStdout(),
    )

    assert exit_code == 1
    assert stdout_text == ""
    assert "Fatal resident protocol failure:" in stderr_text
    assert "stdout transport failed during error emission" in stderr_text
    assert shutdown_calls == ["shutdown"]


def test_resident_main_treats_preload_model_failure_as_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    envelope = _resident_analyze_envelope(tmp_path)
    shutdown_calls: list[str] = []

    def fail_preload():
        raise RuntimeError("preload failed")

    monkeypatch.setattr(module, "preload_model", fail_preload)
    monkeypatch.setattr(module, "shutdown_runtime", lambda: shutdown_calls.append("shutdown"))

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl(envelope),
    )

    assert exit_code == 1
    assert stdout_text == ""
    assert (
        stderr_text
        == "Fatal resident runtime initialization failure: preload failed\n"
    )
    assert shutdown_calls == ["shutdown"]


def test_resident_main_preserves_original_fatal_reason_when_shutdown_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")
    envelope = _resident_analyze_envelope(tmp_path)

    def fail_preload():
        raise RuntimeError("preload failed")

    def fail_shutdown():
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(module, "preload_model", fail_preload)
    monkeypatch.setattr(module, "shutdown_runtime", fail_shutdown)

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl(envelope),
    )

    assert exit_code == 1
    assert stdout_text == ""
    assert stderr_text.splitlines() == [
        "Fatal resident runtime initialization failure: preload failed",
        "Resident runtime cleanup failed during fatal exit: cleanup failed",
    ]


def test_resident_main_returns_non_zero_when_shutdown_cleanup_fails_on_shutdown_message(
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")

    def fail_shutdown():
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(module, "shutdown_runtime", fail_shutdown)

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        _resident_jsonl({"message_type": "shutdown"}),
    )

    assert exit_code == 1
    assert stdout_text == ""
    assert stderr_text == "Resident runtime cleanup failed during shutdown: cleanup failed\n"


def test_resident_main_returns_non_zero_when_shutdown_cleanup_fails_on_eof(
    monkeypatch: pytest.MonkeyPatch,
):
    module = _load_module(ROOT / "templates" / "python_resident_stdio" / "resident_cli.py")

    def fail_shutdown():
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(module, "shutdown_runtime", fail_shutdown)

    exit_code, stdout_text, stderr_text = _run_resident_main(
        module,
        monkeypatch,
        "",
    )

    assert exit_code == 1
    assert stdout_text == ""
    assert stderr_text == "Resident runtime cleanup failed during stdin EOF: cleanup failed\n"
