from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path, PureWindowsPath
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One-shot packaging template")
    parser.add_argument("--request", required=True, help="Absolute path to request JSON.")
    parser.add_argument("--response", required=True, help="Absolute path to response JSON.")
    parser.add_argument(
        "--artifacts-dir",
        required=True,
        help="Absolute path to the output artifacts directory.",
    )
    return parser.parse_args(argv)


def ensure_absolute_path(path_value: str, flag_name: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError(f"{flag_name} must be an absolute path.")
    return path


def require_string(value: Any, error_message: str) -> None:
    if not isinstance(value, str):
        raise ValueError(error_message)


def require_integer(value: Any, error_message: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(error_message)


def validate_segment(segment: Any, index: int) -> None:
    segment_prefix = f"segments[{index}]"
    if not isinstance(segment, dict):
        raise ValueError(f"{segment_prefix} must be a JSON object.")

    require_string(
        segment.get("segment_id"),
        f"{segment_prefix}.segment_id must be a string.",
    )
    require_string(
        segment.get("role"),
        f"{segment_prefix}.role must be a string.",
    )

    audio_path = segment.get("audio_path")
    if not isinstance(audio_path, str) or not Path(audio_path).is_absolute():
        raise ValueError(f"{segment_prefix}.audio_path must be an absolute path.")

    if not isinstance(segment.get("channels"), list):
        raise ValueError(f"{segment_prefix}.channels must be a list.")

    require_integer(
        segment.get("duration_ms"),
        f"{segment_prefix}.duration_ms must be an integer.",
    )
    require_integer(
        segment.get("sample_rate"),
        f"{segment_prefix}.sample_rate must be an integer.",
    )

    if not isinstance(segment.get("metadata"), dict):
        raise ValueError(f"{segment_prefix}.metadata must be an object.")


def load_request(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Request payload must be a JSON object.")
    require_integer(payload.get("protocol_version"), "protocol_version must equal 1.")
    if payload.get("protocol_version") != 1:
        raise ValueError("protocol_version must equal 1.")
    for key in ("request_id", "test_run_id", "toolchain_id"):
        if not isinstance(payload.get(key), str):
            raise ValueError(f"{key} must be a string.")
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("segments must be a non-empty list.")
    for index, segment in enumerate(segments):
        validate_segment(segment, index)
    if not isinstance(payload.get("context"), dict):
        raise ValueError("context must be an object.")
    if not isinstance(payload.get("params"), dict):
        raise ValueError("params must be an object.")
    if not isinstance(payload.get("window_info"), dict):
        raise ValueError("window_info must be an object.")
    return payload


def select_segment(request_payload: dict[str, Any]) -> dict[str, Any]:
    segments = request_payload["segments"]
    segment_index = request_payload["params"].get("segment_index")
    if (
        isinstance(segment_index, int)
        and not isinstance(segment_index, bool)
        and 0 <= segment_index < len(segments)
    ):
        selected = segments[segment_index]
    else:
        selected = segments[0]
    if not isinstance(selected, dict):
        raise ValueError("Selected segment must be a JSON object.")
    audio_path = selected.get("audio_path")
    if not isinstance(audio_path, str) or not Path(audio_path).is_absolute():
        raise ValueError("segment audio_path must be an absolute path.")
    return selected


def preprocess_from_request(request_payload: dict[str, Any]) -> Any:
    segment = select_segment(request_payload)
    # Replace this block with the real audio loading and normalization flow.
    raise NotImplementedError(
        f"Implement preprocessing for segment {segment.get('segment_id', '<unknown>')}."
    )


def run_model(preprocessed: Any) -> Any:
    # Replace this hook with the real inference entrypoint.
    raise NotImplementedError("Implement the model inference call.")


def validate_artifact_relative_path(relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("Artifact relative_path must be a non-empty string.")
    artifact_path = Path(relative_path)
    windows_path = PureWindowsPath(relative_path)
    if artifact_path.is_absolute() or windows_path.anchor:
        raise ValueError("Artifact relative_path must stay relative.")
    if ".." in artifact_path.parts or ".." in windows_path.parts:
        raise ValueError("Artifact relative_path must not traverse upward.")
    return artifact_path


def validate_artifact(artifact: dict[str, Any]) -> Path:
    if not isinstance(artifact, dict):
        raise ValueError("Artifact entries must be JSON objects.")
    artifact_type = artifact.get("type")
    if not isinstance(artifact_type, str) or not artifact_type.strip():
        raise ValueError("Artifact type must be a non-empty string.")
    return validate_artifact_relative_path(artifact.get("relative_path"))


def validate_declared_artifacts(
    artifacts_dir: Path,
    artifacts: list[dict[str, Any]],
) -> None:
    artifacts_root = artifacts_dir.resolve()
    missing_or_unreadable_message = (
        "Artifact relative_path must reference an existing readable file under artifacts_dir."
    )
    for artifact in artifacts:
        relative_path = validate_artifact(artifact)
        try:
            resolved_artifact_path = (artifacts_root / relative_path).resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(missing_or_unreadable_message) from exc
        try:
            resolved_artifact_path.relative_to(artifacts_root)
        except ValueError as exc:
            raise ValueError("Artifact relative_path must resolve under artifacts_dir.") from exc
        if not resolved_artifact_path.is_file():
            raise ValueError(missing_or_unreadable_message)
        try:
            with resolved_artifact_path.open("rb"):
                pass
        except OSError as exc:
            raise ValueError(missing_or_unreadable_message) from exc


def write_artifacts(artifacts_dir: str, inference_output: Any) -> list[dict[str, str]]:
    Path(artifacts_dir).mkdir(parents=True, exist_ok=True)
    # Replace this placeholder with real artifact creation. Only report files
    # that already exist under artifacts_dir, and keep each relative_path safe.
    _ = inference_output
    return []


def build_success_response(
    request_payload: dict[str, Any],
    inference_output: Any,
    artifacts: list[dict[str, str]],
    artifacts_dir: str | Path | None = None,
) -> dict[str, Any]:
    if artifacts_dir is None:
        for artifact in artifacts:
            validate_artifact(artifact)
    else:
        validate_declared_artifacts(Path(artifacts_dir), artifacts)
    # Keep the default template JSON-safe. Only add raw inference output after
    # converting it to JSON-safe types that the host contract explicitly allows.
    _ = inference_output
    ai_result: dict[str, Any] = {
        "ai_label": "PASS",
        "summary": "Replace with a host-meaningful summary.",
        "artifacts": artifacts,
        "extra": {},
    }
    # Only add score/confidence when you have real numeric values to emit.
    result = {
        "protocol_version": 1,
        "request_id": request_payload["request_id"],
        "toolchain_name": "Replace With Toolchain Name",
        "toolchain_version": "0.0.0",
        "ai_result": ai_result,
    }
    return result


def write_response(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def cleanup_stale_response(path: Path) -> None:
    if not path.is_file():
        return
    try:
        path.unlink()
        return
    except PermissionError:
        # Windows commonly blocks deletion of read-only files until the write
        # bit is restored, so retry once after making the file writable.
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    except OSError as exc:
        raise RuntimeError(f"Unable to remove stale response file at {path}: {exc}") from exc
    try:
        path.unlink()
    except OSError as exc:
        raise RuntimeError(f"Unable to remove stale response file at {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    response_path: Path | None = None

    try:
        response_path = ensure_absolute_path(args.response, "--response")
        request_path = ensure_absolute_path(args.request, "--request")
        artifacts_dir = ensure_absolute_path(args.artifacts_dir, "--artifacts-dir")
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        request_payload = load_request(str(request_path))
        preprocessed = preprocess_from_request(request_payload)
        inference_output = run_model(preprocessed)
        artifacts = write_artifacts(str(artifacts_dir), inference_output)
        response_payload = build_success_response(
            request_payload,
            inference_output,
            artifacts,
            artifacts_dir=artifacts_dir,
        )
        write_response(response_path, response_payload)
        return 0
    except Exception as exc:
        cleanup_error: Exception | None = None
        if response_path is not None:
            try:
                cleanup_stale_response(response_path)
            except Exception as cleanup_exc:
                cleanup_error = cleanup_exc
        # Fatal exception path: print to stderr and exit 1.
        message = f"Fatal one-shot failure: {exc}"
        if cleanup_error is not None:
            message = f"{message} Cleanup failure: {cleanup_error}"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
