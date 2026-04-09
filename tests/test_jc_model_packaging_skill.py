import importlib.util
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

LATER_SLICE_FILES = [
    ROOT / "templates" / "python_resident_stdio" / "README.md",
    ROOT / "templates" / "python_resident_stdio" / "resident_cli.py",
    ROOT / "templates" / "python_resident_stdio" / "resident_protocol.md",
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


def test_skill_package_contains_slice_one_required_files():
    missing = [str(path) for path in OWNED_REQUIRED_FILES if not path.is_file()]
    assert missing == []


@pytest.mark.xfail(
    reason="Resident, Windows packaging, and smoke fixtures land in later slices."
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
