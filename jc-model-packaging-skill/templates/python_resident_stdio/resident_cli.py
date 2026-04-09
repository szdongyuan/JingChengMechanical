from __future__ import annotations

import json
import sys
from pathlib import Path, PureWindowsPath
from typing import Any


_MODEL_HANDLE: Any | None = None
_PROCESS_CACHE: dict[str, Any] = {}


class ProtocolError(ValueError):
    """Raised when an inbound resident protocol message is malformed."""


class ProtocolTransportError(RuntimeError):
    """Raised when a protocol message cannot be emitted over stdout."""


def configure_utf8_stdio() -> None:
    # Resident transport is fixed: UTF-8 JSONL over stdin/stdout, with stderr
    # reserved for human-readable diagnostics.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def emit_json_line(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_protocol_line(payload: dict[str, Any]) -> None:
    try:
        emit_json_line(payload)
    except (BrokenPipeError, OSError) as exc:
        raise ProtocolTransportError(
            f"Unable to write protocol output to stdout: {exc}"
        ) from exc


def log_stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def require_string(value: Any, error_message: str) -> None:
    if not isinstance(value, str):
        raise ProtocolError(error_message)


def require_integer(value: Any, error_message: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(error_message)


def validate_segment(segment: Any, index: int) -> None:
    segment_prefix = f"segments[{index}]"
    if not isinstance(segment, dict):
        raise ProtocolError(f"{segment_prefix} must be a JSON object.")
    require_string(segment.get("segment_id"), f"{segment_prefix}.segment_id must be a string.")
    require_string(segment.get("role"), f"{segment_prefix}.role must be a string.")

    audio_path = segment.get("audio_path")
    if not isinstance(audio_path, str) or not Path(audio_path).is_absolute():
        raise ProtocolError(f"{segment_prefix}.audio_path must be an absolute path.")

    if not isinstance(segment.get("channels"), list):
        raise ProtocolError(f"{segment_prefix}.channels must be a list.")
    require_integer(segment.get("duration_ms"), f"{segment_prefix}.duration_ms must be an integer.")
    require_integer(segment.get("sample_rate"), f"{segment_prefix}.sample_rate must be an integer.")

    if not isinstance(segment.get("metadata"), dict):
        raise ProtocolError(f"{segment_prefix}.metadata must be an object.")


def ensure_absolute_path(path_value: Any, field_name: str) -> str:
    if not isinstance(path_value, str):
        raise ProtocolError(f"{field_name} must be a string.")
    if not Path(path_value).is_absolute():
        raise ProtocolError(f"{field_name} must be an absolute path.")
    return path_value


def validate_artifact_relative_path(relative_path: Any) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("Artifact relative_path must be a non-empty string.")
    artifact_path = Path(relative_path)
    windows_path = PureWindowsPath(relative_path)
    if artifact_path.is_absolute() or windows_path.anchor:
        raise ValueError("Artifact relative_path must stay relative.")
    if ".." in artifact_path.parts or ".." in windows_path.parts:
        raise ValueError("Artifact relative_path must not traverse upward.")
    return artifact_path


def validate_artifact(artifact: Any) -> Path:
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


def preload_model() -> Any:
    global _MODEL_HANDLE
    if _MODEL_HANDLE is None:
        # Replace this hook with the real model/runtime preload step so later
        # analyze requests can reuse imports, weights, and device state.
        _MODEL_HANDLE = object()
    return _MODEL_HANDLE


def get_process_cache() -> dict[str, Any]:
    # Keep request-safe warm-path data here if repeated analyze calls benefit
    # from in-process caching.
    return _PROCESS_CACHE


def load_analyze_envelope(envelope: Any) -> tuple[str, dict[str, Any], str]:
    if not isinstance(envelope, dict):
        raise ProtocolError("Inbound message must be a JSON object.")

    message_type = envelope.get("message_type")
    require_string(message_type, "message_type is required and must be a string.")
    if message_type != "analyze":
        raise ProtocolError(f"Unsupported analyze envelope message_type: {message_type}")

    request_id = envelope.get("request_id")
    require_string(request_id, "analyze requires string request_id.")

    request_payload = envelope.get("request")
    if not isinstance(request_payload, dict):
        raise ProtocolError("analyze requires object request.")

    artifacts_dir = ensure_absolute_path(
        envelope.get("artifacts_dir"),
        "artifacts_dir",
    )

    inner_request_id = request_payload.get("request_id")
    require_string(inner_request_id, "request.request_id must be a string.")
    if inner_request_id != request_id:
        raise ProtocolError("request.request_id must equal outer request_id.")

    require_integer(request_payload.get("protocol_version"), "request.protocol_version must equal 1.")
    if request_payload.get("protocol_version") != 1:
        raise ProtocolError("request.protocol_version must equal 1.")

    require_string(request_payload.get("test_run_id"), "request.test_run_id must be a string.")
    require_string(request_payload.get("toolchain_id"), "request.toolchain_id must be a string.")

    segments = request_payload.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ProtocolError("request.segments must be a non-empty list.")
    for index, segment in enumerate(segments):
        validate_segment(segment, index)

    if not isinstance(request_payload.get("context"), dict):
        raise ProtocolError("request.context must be an object.")
    if not isinstance(request_payload.get("params"), dict):
        raise ProtocolError("request.params must be an object.")
    if not isinstance(request_payload.get("window_info"), dict):
        raise ProtocolError("request.window_info must be an object.")

    return request_id, request_payload, artifacts_dir


def select_segment(request_payload: dict[str, Any]) -> dict[str, Any]:
    segments = request_payload["segments"]
    segment_index = request_payload["params"].get("segment_index")
    if (
        isinstance(segment_index, int)
        and not isinstance(segment_index, bool)
        and segment_index >= 0
        and segment_index < len(segments)
    ):
        selected = segments[segment_index]
    else:
        selected = segments[0]
    if not isinstance(selected, dict):
        raise ProtocolError("Selected segment must be a JSON object.")
    return selected


def preprocess_from_request(
    request_payload: dict[str, Any],
    selected_segment: dict[str, Any],
    cache: dict[str, Any],
) -> Any:
    # Replace this block with the real audio loading and normalization flow.
    _ = (request_payload, cache)
    raise NotImplementedError(
        f"Implement preprocessing for segment {selected_segment.get('segment_id', '<unknown>')}."
    )


def run_model(model_handle: Any, preprocessed: Any, cache: dict[str, Any]) -> Any:
    # Replace this hook with the real inference entrypoint.
    _ = (model_handle, preprocessed, cache)
    raise NotImplementedError("Implement the model inference call.")


def write_artifacts(
    artifacts_dir: str,
    inference_output: Any,
    cache: dict[str, Any],
) -> list[dict[str, str]]:
    Path(artifacts_dir).mkdir(parents=True, exist_ok=True)
    # Replace this placeholder with real artifact creation under artifacts_dir.
    # Every emitted artifact entry must use a non-empty descriptive type such as
    # report_json, spectrogram_npy, image_png, or audio_wav.
    # Every relative_path must be non-empty, relative, free of "..", and must never reference files outside the supplied artifacts directory.
    _ = (inference_output, cache)
    return []


def build_result_envelope(
    request_id: str,
    inference_output: Any,
    artifacts: list[dict[str, str]],
    artifacts_dir: str,
) -> dict[str, Any]:
    # Emit success only after every declared artifact already exists, is
    # readable under artifacts_dir, and stays within the supplied directory.
    validate_declared_artifacts(Path(artifacts_dir), artifacts)
    _ = inference_output
    return {
        "message_type": "result",
        "request_id": request_id,
        "response": {
            "protocol_version": 1,
            "request_id": request_id,
            "toolchain_name": "Replace With Toolchain Name",
            "toolchain_version": "0.0.0",
            "ai_result": {
                "ai_label": "PASS",
                "summary": "Replace with a host-meaningful summary.",
                "artifacts": artifacts,
                "extra": {},
            },
        },
    }


def build_error_envelope(request_id: str, error_message: str) -> dict[str, str]:
    return {
        "message_type": "error",
        "request_id": request_id,
        "error": error_message,
    }


def handle_analyze(request_id: str, request_payload: dict, artifacts_dir: str) -> dict:
    model_handle = preload_model()
    cache = get_process_cache()
    selected_segment = select_segment(request_payload)
    preprocessed = preprocess_from_request(request_payload, selected_segment, cache)
    inference_output = run_model(model_handle, preprocessed, cache)
    artifacts = write_artifacts(artifacts_dir, inference_output, cache)
    return build_result_envelope(request_id, inference_output, artifacts, artifacts_dir)


def shutdown_runtime() -> None:
    global _MODEL_HANDLE
    _PROCESS_CACHE.clear()
    # Replace this hook with any runtime-specific cleanup, such as closing a
    # device session or releasing framework resources.
    _MODEL_HANDLE = None


def fatal_exit(message: str) -> int:
    log_stderr(message)
    shutdown_runtime()
    return 1


def main() -> int:
    configure_utf8_stdio()
    try:
        for line in sys.stdin:
            envelope = json.loads(line)
            if not isinstance(envelope, dict):
                raise ProtocolError("Inbound message must be a JSON object.")

            message_type = envelope.get("message_type")
            require_string(message_type, "message_type is required and must be a string.")

            if message_type == "shutdown":
                shutdown_runtime()
                return 0

            request_id, request_payload, artifacts_dir = load_analyze_envelope(envelope)

            try:
                payload = handle_analyze(request_id, request_payload, artifacts_dir)
            except Exception as exc:
                # The default template stays alive after one request-scoped
                # error envelope so later analyze messages can continue. An
                # external host may still choose to discard this process.
                payload = build_error_envelope(request_id, f"Request failed: {exc}")

            try:
                emit_protocol_line(payload)
            except ProtocolTransportError as exc:
                return fatal_exit(f"Fatal resident protocol failure: {exc}")

        shutdown_runtime()
        return 0
    except json.JSONDecodeError as exc:
        return fatal_exit(f"Malformed resident protocol message: {exc}")
    except ProtocolError as exc:
        return fatal_exit(f"Malformed resident protocol message: {exc}")


if __name__ == "__main__":
    sys.exit(main())
