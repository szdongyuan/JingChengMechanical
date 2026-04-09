from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
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


def load_request(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Request payload must be a JSON object.")
    if payload.get("protocol_version") != 1:
        raise ValueError("protocol_version must equal 1.")
    for key in ("request_id", "test_run_id", "toolchain_id"):
        if not isinstance(payload.get(key), str):
            raise ValueError(f"{key} must be a string.")
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("segments must be a non-empty list.")
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
    if isinstance(segment_index, int) and 0 <= segment_index < len(segments):
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


def validate_artifact_relative_path(relative_path: str) -> None:
    artifact_path = Path(relative_path)
    if artifact_path.is_absolute():
        raise ValueError("Artifact relative_path must stay relative.")
    if ".." in artifact_path.parts:
        raise ValueError("Artifact relative_path must not traverse upward.")


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
) -> dict[str, Any]:
    for artifact in artifacts:
        validate_artifact_relative_path(artifact["relative_path"])
    result = {
        "protocol_version": 1,
        "request_id": request_payload["request_id"],
        "toolchain_name": "Replace With Toolchain Name",
        "toolchain_version": "0.0.0",
        "ai_result": {
            "ai_label": "PASS",
            "score": None,
            "summary": "Replace with a host-meaningful summary.",
            "confidence": None,
            "artifacts": artifacts,
            "extra": {
                "raw_output": inference_output,
            },
        },
    }
    return result


def write_response(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def cleanup_stale_response(path: Path) -> None:
    if path.exists():
        path.unlink()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    request_path = ensure_absolute_path(args.request, "--request")
    response_path = ensure_absolute_path(args.response, "--response")
    artifacts_dir = ensure_absolute_path(args.artifacts_dir, "--artifacts-dir")

    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        request_payload = load_request(str(request_path))
        preprocessed = preprocess_from_request(request_payload)
        inference_output = run_model(preprocessed)
        artifacts = write_artifacts(str(artifacts_dir), inference_output)
        response_payload = build_success_response(request_payload, inference_output, artifacts)
        write_response(response_path, response_payload)
        return 0
    except Exception as exc:
        cleanup_stale_response(response_path)
        # Fatal exception path: print to stderr and exit 1.
        print(f"Fatal one-shot failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
