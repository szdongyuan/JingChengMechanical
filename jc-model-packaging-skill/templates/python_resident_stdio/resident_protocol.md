# Resident Stdio Protocol Contract

This resident packaging mode uses JSON Lines over `stdin` and `stdout`. Each
inbound line must be one JSON object. Emit no extra stdout text beyond protocol
JSON lines. `stderr` is reserved for human-readable diagnostics only.

## Required Inbound Envelopes

The resident docs and templates must define this exact `analyze` input
envelope:

```json
{
  "message_type": "analyze",
  "request_id": "req_20260409_120000_abcd1234",
  "request": {
    "protocol_version": 1,
    "request_id": "req_20260409_120000_abcd1234",
    "test_run_id": "run_001",
    "toolchain_id": "example-toolchain",
    "segments": [
      {
        "segment_id": "segment_001",
        "role": "device_1",
        "audio_path": "D:/absolute/path/input.wav",
        "channels": [0],
        "duration_ms": 4000,
        "sample_rate": 44100,
        "metadata": {}
      }
    ],
    "context": {},
    "params": {},
    "window_info": {}
  },
  "artifacts_dir": "D:/absolute/path/artifacts"
}
```

The resident docs and templates must define this exact `shutdown` input
envelope:

```json
{
  "message_type": "shutdown"
}
```

## Required Outbound Envelopes

The resident docs and templates must define this exact success output
envelope:

```json
{
  "message_type": "result",
  "request_id": "req_20260409_120000_abcd1234",
  "response": {
    "protocol_version": 1,
    "request_id": "req_20260409_120000_abcd1234",
    "toolchain_name": "Example Toolchain",
    "toolchain_version": "1.0.0",
    "ai_result": {
      "ai_label": "PASS",
      "score": 0.91,
      "summary": "Inference succeeded.",
      "confidence": 0.88,
      "artifacts": [
        {
          "type": "report_json",
          "relative_path": "report.json"
        }
      ],
      "extra": {}
    }
  }
}
```

The resident docs and templates must define this exact request-scoped failure
envelope:

```json
{
  "message_type": "error",
  "request_id": "req_20260409_120000_abcd1234",
  "error": "human-readable summary"
}
```

## Envelope Rules

- Inbound top-level value must be a JSON object.
- `message_type` is required and must be a string.
- `analyze` requires string outer `request_id`.
- `analyze` requires object `request`.
- `analyze` requires string `artifacts_dir`.
- `artifacts_dir` must be an absolute path.
- `request.request_id` must equal outer `request_id`.
- `request.protocol_version` must equal `1`.
- `request.test_run_id` is required and must be a string.
- `request.toolchain_id` is required and must be a string.
- `request.segments` is required, must be a non-empty list, and each segment
  must satisfy the same field rules as one-shot mode.
- `request.context` is required and must be an object.
- `request.params` is required and must be an object.
- `request.window_info` is required and must be an object.
- `request.params.segment_index`, if present, must be a non-negative integer.
- If `request.params.segment_index` is absent, invalid, or out of range, the
  default template behavior is to select `request.segments[0]`.
- `shutdown` expects no output line.

## Stdout, Stderr, And Error Lifecycle

- No extra stdout output is allowed outside protocol JSON lines.
- `stderr` is human-readable only and is not a protocol channel.
- Malformed inbound JSON or malformed protocol envelopes are logged to `stderr`
  and terminate the process non-zero.
- A valid request-scoped analyze failure emits exactly one `error` envelope tied
  to the stable outer `request_id`.
- After a valid request-scoped `error` envelope, the default resident template
  behavior is to remain alive and continue accepting later `analyze` messages.

## Artifact Rules

- The host provides `artifacts_dir` per request.
- Artifacts live under the host-provided absolute `artifacts_dir`.
- Success responses report artifacts in `response.ai_result.artifacts`.
- Each `relative_path` must stay relative to `artifacts_dir`.
- Artifact `type` is descriptive metadata, not a host-parsed enum.
