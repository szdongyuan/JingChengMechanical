# One-Shot Protocol Contract

## CLI

Required arguments:

- `--request`
- `--response`
- `--artifacts-dir`

Rules:

- `--request`, `--response`, and `--artifacts-dir` must be absolute paths.
- The tool may create `--artifacts-dir`.
- Failure to read `--request`, write `--response`, or create or write
  `--artifacts-dir` is a fatal error.
- Fatal failure behavior:
  - print a human-readable error to `stderr`
  - exit non-zero
  - do not write a protocol success response
  - if a stale `--response` file already exists, remove the stale file on fatal
    failure

## Request JSON Example

```json
{
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
}
```

## Success Response JSON Example

```json
{
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
```

## Request Rules

- The top-level request value must be a JSON object.
- `protocol_version` is required and must equal `1`.
- `request_id` is required and must be a string.
- `test_run_id` is required and must be a string.
- `toolchain_id` is required and must be a string.
- `segments` is required and must be a non-empty list.
- `context` is required and must be an object.
- `params` is required and must be an object.
- `window_info` is required and must be an object.
- Use `params.segment_index` when it is a valid non-negative integer inside the
  segment list bounds.
- If `params.segment_index` is absent, invalid, or out of range, use
  `segments[0]`.

## Segment Rules

Each segment must be a JSON object with:

- string `segment_id`
- string `role`
- absolute string `audio_path`
- list `channels`
- integer `duration_ms`
- integer `sample_rate`
- object `metadata`

## Response Rules

- The success response must be a JSON object.
- `toolchain_name` is required and must be a string.
- `toolchain_version` is required and must be a string.
- `ai_result` is required and must be an object.
- `ai_result.ai_label` is required and must be a string.
- `score`, if present, must be numeric.
- `confidence`, if present, must be numeric.
- `summary`, if present, must be a string.
- `extra`, if present, must be an object.
- `artifacts`, if present, must be a list of objects.

## Artifact Safety Rules

Each artifact must have this shape:

```json
{
  "type": "report_json",
  "relative_path": "report.json"
}
```

Artifact rules:

- `type` must be a non-empty string.
- `relative_path` must be a non-empty string.
- `relative_path` must stay relative, not absolute.
- `relative_path` must not contain `..`.
- Every listed artifact must already exist under `--artifacts-dir` when the
  success response is emitted.
- Every listed artifact must be readable by the host at response time.
- Never reference files outside the supplied artifacts directory.
